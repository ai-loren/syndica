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
- **回调跑在 publish 那次调用的上下文里**：同一个任务，或由它 `create_task` 出来的任务（新任务
  复制当前 Context）。回调因此读得到调用方在 publish 之前设下的 ContextVar，而 Runtime 正是靠
  一个 ContextVar 表达「这一次 send 允不允许把目标叫醒」。改由一个早已存在的后台任务派发回调的
  实现读不到那一位，抑制于是静默失效——一条消息叫醒了一个本该继续睡的 Agent，不报任何错。
- **回调列表每次 publish 现读**：在首次 publish 时把它冻结成快照的实现满足上面每一条，却让此后
  注册的回调永远不被调用。共享 broker 上这意味着第一次 missing-queue publish 之后构造的
  Runtime 复活不了自己的 ZOMBIE。
- **取消一次 `take_by_correlation` 必须当场生效**：`cancel()` 之后这次调用要么已经返回，要么当场以
  `CancelledError` 结束，不得接着等到自己的超时。`Runtime.wait_for` 在等待被中止或被顶掉时正是取消
  它，而中止落在轮的中间——取消不当场生效，`abort` / `shutdown` / `despawn` 就全都要挂满整个 waiter
  超时，而超时可以是几小时。**内层等待要用 `asyncio.timeout`，不要用 `asyncio.wait_for`**：3.11 的
  `wait_for` 把内层协程包成 Task，内层那个 future 已经就绪时到达的取消被它当成「结果已到」吃掉
  （`except CancelledError: if fut.done(): return fut.result()`），这次取此后不可取消。一条**不
  匹配**的消息投进来之后到达的取消确定命中这个窗口，不是概率事件。
- **取消一次 `take_by_correlation` 不得吃掉匹配的那条消息**：要么它已经把消息交回调用方，要么这条
  消息仍在**普通消费路径**上——即 `consume` 与 `wait_for_messages` 看得见它，没有第三种结局。内核
  依赖的是这一条而不是「再 take 一次还取得到」：被中止的那次等待返回之后，这条答复退回成一条普通
  消息，由门在下一轮开始前 `consume` 进上下文；只对 take 可见的实现会让 `wait_for_messages` 答
  False、`consume` 交出空列表，那条答复于是静默消失，而 `publish` 早已回过 `ok=True`。先把消息摘出
  邮箱、再 await（ack、回执、审计写入）然后才返回的实现满足上面每一条，却会在取消落进那个窗口时
  销毁一条答复。最省的满足方式是**摘掉与返回之间不留挂起点**。Redis Streams 排不出这个写法
  （`XREADGROUP` 与 `XACK` 之间天然隔着一次往返），只能用「读了但未确认即可重取」的 pending entry
  语义，而那时**该实现的 `consume` 与 `wait_for_messages` 必须先读自己的 pending**
  （`XREADGROUP ... STREAMS key 0`，或 `XAUTOCLAIM`），未确认的条目才真的回到了普通消费路径上。
- **等待上限是方法参数，不是让调用方在外面套 `asyncio.timeout`**：远端实现要把它下推给服务端
  （Redis `XREAD BLOCK`），在本地包一层取消域只会把已经阻塞住的服务端连接留在那里。ruff 的
  ASYNC109 因此在本模块与内存实现里按文件豁免（见 pyproject）。

抛异常还是返回，取决于调用方的意图能否表达：**意图无法表达的操作抛异常，后置条件已经成立的
操作成功返回**。`ensure_queue` 队列已存在时成功（「队列存在」已经成立），`remove_queue` 队列
不存在时也成功（「队列不存在」已经成立），`off_missing_queue` 摘一条从未注册过的回调时同理。
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

        publish 与 remove_queue 重叠时必须存在明确的线性化顺序：若 remove_queue 先摘掉邮箱，
        publish 必须返回 ok=False；若 publish 先写入，remove_queue 可以随后按其明文契约丢弃未消费
        消息。最终确认邮箱存在与写入消息之间不得让出，否则会向已经摘掉的旧邮箱写入并返回成功。
        远端实现用事务或原子脚本把检查与写入收成一个线性化操作。
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
        阻塞到超时——立刻返回 False 会把轮次循环从阻塞等待变成忙轮询。

        邮箱不存在时立刻返回 False，不等待：没有邮箱就没有「等它非空」可言，占满调用方交来的
        超时只是白等，而 timeout=None 时是永久挂死。

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

        **本调用被取消时，取消要当场生效，且匹配的那条消息必须仍在普通消费路径上**（`consume` 与
        `wait_for_messages` 看得见它）——两条都见模块 docstring。调用方在等待被中止或被顶掉时取消
        它：取消被内层的 `asyncio.wait_for` 吃掉就是挂满整个超时，而摘掉与返回之间留了挂起点就是
        静默销毁一条答复。
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

        调用时机、顺序、跑在哪个上下文里、列表何时读，见模块 docstring——四者都是契约的一部分，
        不是实现的自由度。
        """
        ...

    def off_missing_queue(self, cb: MissingQueueCallback) -> None:
        """摘掉一条已注册的回调。没注册过是 no-op。

        注册没有配套的注销，共享 broker 上每个已经关停的 Runtime 就在链上留一条永不回收的死
        回调，并靠那条引用把整个 Runtime 连同它持有的 Agent 实例一起钉在内存里。
        """
        ...
