"""TraceStore：trace 的存储契约（M3 D6），第五个 Store Protocol（spec §4.4 / §13.3）。

所有实现共有的不变式：

- **只追加**：一个 trace 由 `begin` 开始、`finish` 结束；其间 `append` 的 span 与边、
  `put_payload` 的自由文本只增不改。没 `begin` 过、已经 `finish`、或已经 `delete` 的 trace 上的写
  （含空的 `append`）报 `ContractViolation`。
- **拒绝即无副作用**：报错的写一项都不写——混着一条坏边的那批 span 也不写，被拒的 `put_payload`
  读不回来，被拒的 `finish` 不改结局。
- **自由文本只收 `RedactedText`**：`put_payload` 与 `put_replay` 运行时检查类型，收到普通 `str` 报
  `ContractViolation`。脱敏发生在写入之前，store 不再判断内容。
- **一次写是原子的**：一次 `append` 整批出现、不被别的 `append` 插进来，批与批按调用顺序读回；
  同一个 trace id 并发 `begin`、并发 `put_replay`，恰好一个成功。远端实现的检查与写入之间隔着
  往返，要靠事务或条件写（`SET NX`、`INSERT … ON CONFLICT`）做到。
- **读到的是快照**：改读回的对象（含嵌套的属性、列表、事件与 `traces()` 交回的列表），再读不受
  影响；`append` 返回之后调用方再改传进来的对象，同样不影响已经存下的。
- **读己所写**：写操作返回后，紧接着的读必须看到它；`get`、`find`、`traces()` 读到的是同一份头。

读是提问不是要求：没有这个 trace 时 `get` / `payload` / `replay` 交回 None，`spans` / `edges` 交回空
列表；`delete` 一个不存在的 trace 是 no-op，删掉的 trace id 可以重新 `begin`。payload 的引用属于它的
trace：拿别的 trace 的 id 读同一个摘要交回 None。读回的自由文本是普通 `str`（不是 `RedactedText`）：
要再写出去（导出给第三方）就再经一次 `encode_redacted`，脱敏是幂等的；读回的内容与引用的 sha256
对不上时（文件被改过）报 `ContractViolation`，不交回改过的内容。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from syndica.kernel.redaction import RedactedText
from syndica.kernel.trace import CausalityEdge, PayloadRef, Span, SpanStatus, TraceHeader

__all__ = ['TraceStore']


@runtime_checkable
class TraceStore(Protocol):
    async def begin(self, header: TraceHeader) -> None:
        """开始一个 trace。同一个 trace id 第二次 `begin` 报 `ContractViolation`；`header` 的
        `ended_ns` 与 `status` 必须是 None（结局只由 `finish` 写）。"""
        ...

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        """追加一批 span 与边，存的是它们的深拷贝（含事件）。先校验整批（每一项的类型），全部通过
        才写。"""
        ...

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        """存一段自由文本，交回它的引用。引用是内容 UTF-8 编码的 sha256，由 store 算；
        同一个 trace 里内容相同的只存一份，交回同一个引用。"""
        ...

    async def put_replay(self, trace_id: str, document: RedactedText) -> None:
        """存回放文档。每个 trace 至多一份：第二次报 `ContractViolation`。"""
        ...

    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        """写下结局。`status` 不是 `SpanStatus`、`ended_ns` 不是不早于开始时间的整数时报
        `ContractViolation`、trace 仍开着。写下之后，这个 trace 上的写（含再一次 `finish`）报
        `ContractViolation`。"""
        ...

    async def traces(self) -> list[TraceHeader]:
        """全部 trace 的头，按开始时间、再按 trace id 排序。"""
        ...

    async def find(self, prefix: str) -> TraceHeader | None:
        """按 trace id 前缀找。恰好一个时交回它，一个都没有时交回 None；不止一个时报 `ConfigError`，
        列出命中的 id。`prefix` 是 1 到 32 个小写十六进制字符，否则报 `ConfigError`。"""
        ...

    async def get(self, trace_id: str) -> TraceHeader | None: ...

    async def spans(self, trace_id: str) -> list[Span]: ...

    async def edges(self, trace_id: str) -> list[CausalityEdge]: ...

    async def payload(self, ref: PayloadRef) -> str | None: ...

    async def replay(self, trace_id: str) -> str | None: ...

    async def delete(self, trace_id: str) -> None:
        """删掉整个 trace：头、span、边、payload 与回放文档。"""
        ...
