"""MemoryStore：分层记忆的存储契约（spec §8.9 / §4.4）。

两阶段：`record` 记短期（按 (scope, workspace) 单调发 `seq`）；`consolidate` 把短期整理成长期段落、
乐观并发换入。两层次由 `MemoryScope` + `workspace` 表达。

与 `SessionStore` / `AgentRegistry` 同两条不变式：

- **读己所写**：写返回后紧接着的读必须看到它。
- **拒绝即无副作用**：抛异常的写不得留下部分写入。

写入面只收脱敏铸造过的类型（`memory_redaction` 的输出），第三方 store 绕不过脱敏与不可信不进全局。
`consolidate` 用 `expected_version` 做乐观并发：版本不符显式报冲突，不静默覆盖（spec §6.4）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from syndica.kernel.memory_redaction import RedactedMemoryEntry, RedactedMemorySections
from syndica.kernel.memory_types import MemoryEntry, MemoryRecord, MemoryScope

__all__ = ['MemoryStore']


@runtime_checkable
class MemoryStore(Protocol):
    async def record(self, entry: RedactedMemoryEntry) -> int:
        """记一条短期记忆，返回 store 发的 `seq`（按 (scope, workspace) 单调）。只收脱敏类型。"""
        ...

    async def entries(
        self, scope: MemoryScope, workspace: str | None, *, after_seq: int = 0
    ) -> list[MemoryEntry]:
        """某 (scope, workspace) 里 `seq > after_seq` 的短期记忆，按 `seq` 升序。整理与注入用它。"""
        ...

    async def read(self, scope: MemoryScope, workspace: str | None) -> MemoryRecord | None:
        """某 (scope, workspace) 的长期整理结果；没整理过时是 None。返回快照，改它不影响存储。"""
        ...

    async def consolidate(self, sections: RedactedMemorySections, *, expected_version: int) -> int:
        """把长期段落原子换入，返回新 `version`。`expected_version` 与当前不符时报冲突、不覆盖
        （乐观并发，spec §6.4）。`through_seq` 一并写入。只收脱敏类型。"""
        ...
