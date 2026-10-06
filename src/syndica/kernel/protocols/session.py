"""SessionStore：会话可序列化状态的存储契约（spec §4.4）。

它存一次 `run` / `chat` 根对话的轨迹与快照，供跨进程恢复。实现与 `AgentRegistry` 共两条不变式：

- **读己所写**：写操作返回后，紧接着的读必须看到它。
- **拒绝即无副作用**：抛异常的写操作不得留下部分写入。

三条读写纪律（spec §4.4）由本契约的用例保证：

- 会话正文可达数十兆。**只判存在性必须用 `exists()`；只需要一两个元数据字段必须用 `load_meta()`。**
  实现不得在这两条路径上把正文读进内存。
- **归档序号只增不补**：`archive` 永远取当前最大值 +1，出现空洞不回填。
- 单会话的查询不建立在 `list_metas()` 上（它按过滤条件裁剪）；按 id 取用 `load` / `load_meta`。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from syndica.kernel.session_redaction import RedactedContent, RedactedSessionRecord
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus

__all__ = ['SessionStore']


@runtime_checkable
class SessionStore(Protocol):
    async def exists(self, sid: str) -> bool:
        """这个会话在不在。只判存在用它，不要 `load` 整份正文。"""
        ...

    async def load_meta(self, sid: str) -> SessionMeta | None:
        """只取元数据（不读正文）；不存在时是 None。"""
        ...

    async def load(self, sid: str) -> SessionRecord | None:
        """取整份记录（含正文）；不存在时是 None。返回快照，调用方改它不影响存储。正文已脱敏。"""
        ...

    async def save(self, sid: str, record: RedactedSessionRecord) -> None:
        """写入或覆盖会话。只收脱敏过的记录（`redacted_record` 的输出），第三方 store 绕不过脱敏。
        写入的是深拷贝；`record.record.meta.sid` 与 `sid` 不一致时抛异常（否则记录找不回）。"""
        ...

    async def archive(self, sid: str, record: RedactedSessionRecord) -> int:
        """把当前内容归档成一份历史副本，返回这次的归档序号（当前最大值 +1，不回填空洞），并把
        live 记录的 `SessionMeta.archived` 前移。只收脱敏过的记录。

        实现可以只留最近若干份（内置的两个留 20 份：每次压缩归档一份完整前像，跑几天的会话会一直长
        下去）；删掉的序号 `read_archived` 交回 None，且序号永不复用。"""
        ...

    async def list_metas(
        self,
        *,
        status: SessionStatus | None = None,
        agent: str | None = None,
        workspace: str | None = None,
    ) -> list[SessionMeta]:
        """按闭集过滤列出会话元数据（不读正文），按 `updated_at` 倒序。只认这三个过滤键。"""
        ...

    async def replace_content(self, sid: str, content: RedactedContent) -> None:
        """只替换正文（压缩、恢复重建后写回），不动元数据其余字段，但 `updated_at` 要前移。只收脱敏
        过的内容。会话不存在时抛异常。"""
        ...

    async def delete(self, sid: str) -> None:
        """删除会话（`sessions rm`）。不存在时是 no-op，不抛异常。"""
        ...

    async def read_archived(self, sid: str, seq: int) -> SessionRecord | None:
        """读回一份归档副本（§8.8「出问题能回看压缩前的状态」）；没有那个序号时是 None。"""
        ...
