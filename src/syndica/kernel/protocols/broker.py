"""MessageBroker：Agent 邮箱的存储契约。

所有实现共有以下性质，契约套件逐条依赖它们：

- **单个邮箱内 FIFO**：同一 agent 的消息按 publish 顺序被取出。跨邮箱不保证任何顺序。
- **consume 恰好一次**：取出即从邮箱移除，同一条消息不会被交付两次。
- **消息是快照**：publish 返回后修改调用方手里的 Message 不得改变已入队的内容，consume 与
  take_by_correlation 交回的对象同理与存储解耦。远端实现靠序列化天然满足，内存实现必须显式
  深拷贝——否则同一份代码换个后端就换个行为，而分叉只在换的那一刻才暴露。
- **投递失败返回 DeliveryResult，不抛异常**：调用方靠 ok / reason 区分可重试的瞬时错误。
  publish 是唯一会失败的写操作，它以返回值报告失败。
- **on_missing_queue 每次 publish 最多触发一轮**：按注册顺序调用，停在第一个返回 True 的回调
  上，不重复调用已经调过的回调。
- **等待上限是方法参数，不是让调用方在外面套 `asyncio.timeout`**：远端实现要把它下推给服务端
  （Redis `XREAD BLOCK`），在本地包一层取消域只会把已经阻塞住的服务端连接留在那里。ruff 的
  ASYNC109 因此在本模块与内存实现里按文件豁免（见 pyproject）。

抛异常还是返回，取决于调用方的意图能否表达：**意图无法表达的操作抛异常，后置条件已经成立的
操作成功返回**。`ensure_queue` 队列已存在时成功（「队列存在」已经成立），`remove_queue` 队列
不存在时也成功（「队列不存在」已经成立）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from syndica.kernel.message import DeliveryResult, Message

__all__ = ['MessageBroker', 'MissingQueueCallback']

MissingQueueCallback = Callable[[str], Awaitable[bool]]


@runtime_checkable
class MessageBroker(Protocol):
    async def ensure_queue(self, agent_id: str) -> None:
        """建立邮箱。已存在时是 no-op，且不得丢弃已入队的消息。"""
        ...

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        """投递一条消息，成功时 message_id 必须逐字等于 message.id。

        id 由调用方在客户端生成，回执原样回声它，调用方才能把回执对上自己发的那一条；换成
        后端自己的 entry id 就对不上了，而调用方手里没有别的对照物。

        邮箱不存在时先走 on_missing_queue 回调；回调之后仍不存在则返回 ok=False。
        """
        ...

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        """按 FIFO 取出至多 max_n 条并移出邮箱；max_n <= 0 表示全取。

        邮箱不存在时返回空列表。
        """
        ...

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool:
        """等到邮箱非空，返回 True；超时返回 False。timeout=None 表示无限等待。

        邮箱当前非空时立刻返回 True，因此 timeout=0 就是纯轮询（Redis 实现注意 `BLOCK 0`
        在 Redis 里是「永久阻塞」，语义相反，必须自己拦下 timeout=0）。邮箱为空时必须真的
        阻塞到超时——立刻返回 False 会把轮次循环从阻塞等待变成忙轮询。邮箱不存在时返回 False。

        等待期间邮箱被 remove_queue 删掉时立刻返回 False，不等到超时：这是一个问句，答不出
        「有消息」就答 False，与超时同一个答案，调用方下一步都是回去重读该 agent 的状态。
        """
        ...

    async def take_by_correlation(
        self, agent_id: str, correlation_id: str, timeout: float | None
    ) -> Message | None:
        """取出 correlation_id 匹配的第一条消息，其余留在邮箱里；超时返回 None。

        刻意不接受任意谓词：谓词无法下推到远端队列，分布式实现只能全量取回本地过滤再放回，
        既慢又有竞态。

        没有匹配项时必须阻塞等新消息入队，而不是反复重扫邮箱：邮箱里留着不匹配的消息时
        「邮箱非空」恒为真，拿它当唤醒条件就退化成忙轮询——结果照样正确，只有 CPU 时间看得出来。

        timeout 是从调用那一刻起算的整段上限，不因期间来了不匹配的消息而顺延：每醒一次就重算
        deadline 的话，稳定的非匹配流量会让它永不返回。

        邮箱不存在时立刻返回 None，不等待；等待期间邮箱被 remove_queue 删掉时同样返回 None。
        与 wait_for_messages 一致：问句答不出就答 None，不抛异常，也不另造第三种返回值。
        """
        ...

    async def remove_queue(self, agent_id: str) -> None:
        """删除邮箱，未消费的消息一并丢弃。邮箱不存在时是 no-op，不抛异常。

        之后重建的同名邮箱必须是全新的：残留的唤醒信号会让 wait_for_messages 立刻返回。

        必须唤醒此刻还停在这个邮箱上的等待者，让它们各自答 False / None：它们等的信号此后
        再没人会置位，timeout=None 是公开签名的一部分，不唤醒就是永久挂死。按 key 寻址的远端
        实现天然会看见重建后的流，内存实现不显式唤醒就在这里与它分叉。
        """
        ...

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        """注册按需激活回调。「队列不存在」是懒启动与 ZOMBIE 复活的触发点。

        回调返回 True 表示已认领并把邮箱建好，False 表示放弃、让下一个回调接手。
        """
        ...
