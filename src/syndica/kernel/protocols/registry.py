"""AgentRegistry：Agent 可序列化状态的存储契约。

它只回答「这个 Agent 处于什么状态」；「这个 Agent 的对象在哪」由进程内的
LocalAgentTable 回答。两者分离是存储层可替换的地基（spec §4.3）。

所有实现共有两条不变式：

- **读己所写**：写操作返回后，紧接着的读必须看到它。从落后副本返回旧值不合规。
- **拒绝即无副作用**：抛异常的写操作不得留下部分写入。

抛异常还是返回，取决于调用方的意图能否表达：**意图无法表达的操作抛异常，后置条件已经成立的
操作成功返回**。`register` 重复 id 抛（调用方要的是「创建」），`update` 缺条目抛（没有东西可写），
`unregister` 缺条目成功（「不在注册表里」已经成立）；`get` 与 `compare_and_set_status` 是提问
不是要求，未知条目分别返回 `None` 与 `False`。

**CAS 必须是原子的**（spec §4.4 / §13.3）：读状态与写状态之间不得插入一次让出。远端 store 的
往返是必然的让出点，所以「读完再写」看起来对、并发下会丢转换（挂起与中止相撞就是这一条）。可接受
的实现路径：Lua EVAL 把 GET+SET 收进一次脚本、WATCH / MULTI / EXEC 乐观锁、或一次把比较与赋值
收成一趟往返的事务。内存实现必须把读与写放在同一次不让出的临界区里。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from syndica.kernel.agent_types import AgentInfo, AgentStatus

__all__ = [
    'AgentRegistry',
    'UPDATE_PROTECTED_FIELDS',
    'UPDATE_PROTECTED_FIELD_OWNERS',
    'UPDATE_WRITABLE_FIELDS',
]

# 两个集合必须构成 AgentInfo 字段的一个划分：AgentInfo 新增字段必须落进其中之一，
# 否则 update() 该不该写它无人回答。
#
# 可写 = 只有一个写者，无条件写不会覆盖掉别人的写入。
UPDATE_WRITABLE_FIELDS: frozenset[str] = frozenset(
    {'agent_type', 'heartbeat_at', 'metadata', 'name', 'owner', 'session_id'}
)
# 受保护 = 另有专用机制拥有它，逐字段的机制与理由见 UPDATE_PROTECTED_FIELD_OWNERS。
UPDATE_PROTECTED_FIELDS: frozenset[str] = frozenset(
    {'agent_id', 'children', 'parent_id', 'status', 'created_at', 'flags'}
)
# 拒绝写一个受保护字段时必须报出谁才是它的写入路径，否则调用方只知道被拒、不知道该走哪条路。
# 键集必须与 UPDATE_PROTECTED_FIELDS 逐字段对齐。
UPDATE_PROTECTED_FIELD_OWNERS: Mapping[str, str] = MappingProxyType(
    {
        'agent_id': 'register / unregister——主键随条目一起创建与销毁',
        'children': 'register / unregister——它们同时更新父与子两端',
        'parent_id': 'register / unregister——它们同时更新父与子两端',
        'status': 'compare_and_set_status——状态有并发写者，只能靠 CAS 不丢转换',
        'created_at': (
            'register——随条目诞生，不该被无条件写；heartbeat_at 是租约活性不是创建时间，'
            '拿它当年龄会把刚复活过的老 Agent 判成新的'
        ),
        'flags': 'Runtime.spawn——Flag 在 Agent 创建时解析成快照，此后不可无条件改写',
    }
)


@runtime_checkable
class AgentRegistry(Protocol):
    async def register(self, info: AgentInfo) -> None:
        """写入新条目并把它挂到父的 children 上。

        agent_id 已存在、或 parent_id 非 None 而该父不在注册表中时抛异常；后者包含空串——
        空串是「声称有父但父未注册」，不是「无父」，无父只有 None 一种写法。
        """
        ...

    async def get(self, agent_id: str) -> AgentInfo | None:
        """返回快照。调用方修改返回值（含 metadata / children 等嵌套容器）不得影响存储。"""
        ...

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        """状态转换。expected 不匹配或 agent 不存在时返回 False，不抛异常。

        读与写必须是一次原子操作：两者之间让出控制权（`await`、远端往返）会让并发转换丢失。
        可接受的实现路径：Lua EVAL、Redis WATCH、或一次事务。
        """
        ...

    async def update(self, agent_id: str, **fields: Any) -> None:
        """无条件写 `UPDATE_WRITABLE_FIELDS` 里的字段。

        `UPDATE_PROTECTED_FIELDS` 里的字段与两个集合之外的字段名一律抛异常，
        且拒绝时一个字段都不写。agent_id 不存在时抛异常。写入的是调用方取值的深拷贝；
        方法返回后调用方继续修改原容器，不得改变已经存下的字段。
        """
        ...

    async def children_of(self, agent_id: str) -> list[str]:
        """返回子代 id 的快照；agent 不存在时返回空列表。"""
        ...

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]:
        """返回快照列表，语义同 `get`。不保证顺序。"""
        ...

    async def unregister(self, agent_id: str) -> None:
        """删除条目并把它从父的 children 上摘掉。agent 不存在时是 no-op，不抛异常。"""
        ...
