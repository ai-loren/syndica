"""MemoryStore 的内存实现：单机默认，零依赖。"""

from __future__ import annotations

import copy
import time

from syndica.kernel.errors import ContractViolation
from syndica.kernel.memory_redaction import RedactedMemoryEntry, RedactedMemorySections
from syndica.kernel.memory_types import MemoryEntry, MemoryRecord, MemoryScope

__all__ = ['InMemoryMemoryStore', 'MemoryConflict']


class MemoryConflict(ContractViolation):
    """`consolidate` 的 `expected_version` 与当前不符：显式冲突，不静默覆盖（spec §6.4）。"""


def _key(scope: MemoryScope, workspace: str | None) -> tuple[str, str | None]:
    # 全局层忽略 workspace：全局只有一份，不按工作区分。
    return (scope.value, None if scope is MemoryScope.GLOBAL else workspace)


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._short: dict[tuple[str, str | None], list[MemoryEntry]] = {}
        self._long: dict[tuple[str, str | None], MemoryRecord] = {}

    async def record(self, entry: RedactedMemoryEntry) -> int:
        inner = entry.entry
        key = _key(inner.scope, inner.workspace)
        log = self._short.setdefault(key, [])
        folded = self._long.get(key)
        seq = max(log[-1].seq if log else 0, folded.through_seq if folded else 0) + 1
        stored = copy.deepcopy(inner)
        stored.seq = seq
        log.append(stored)
        return seq

    async def entries(
        self, scope: MemoryScope, workspace: str | None, *, after_seq: int = 0
    ) -> list[MemoryEntry]:
        log = self._short.get(_key(scope, workspace), [])
        return [copy.deepcopy(entry) for entry in log if entry.seq > after_seq]

    async def read(self, scope: MemoryScope, workspace: str | None) -> MemoryRecord | None:
        record = self._long.get(_key(scope, workspace))
        return copy.deepcopy(record) if record is not None else None

    async def consolidate(self, sections: RedactedMemorySections, *, expected_version: int) -> int:
        key = _key(sections.scope, sections.workspace)
        current = self._long.get(key)
        version = current.version if current is not None else 0
        if version != expected_version:
            raise MemoryConflict(
                expected=f'consolidate 时长期记忆版本是 {expected_version}',
                received=f'当前版本是 {version}（有别的写者先一步整理了）',
                next_action='重新 read 取最新 version 与 through_seq，合并后再 consolidate',
            )
        self._long[key] = MemoryRecord(
            scope=sections.scope,
            workspace=sections.workspace if sections.scope is not MemoryScope.GLOBAL else None,
            sections=copy.deepcopy(sections.sections),
            through_seq=sections.through_seq,
            version=version + 1,
            updated_at=time.time(),
        )
        log = self._short.get(key, [])
        self._short[key] = [entry for entry in log if entry.seq > sections.through_seq]
        return version + 1
