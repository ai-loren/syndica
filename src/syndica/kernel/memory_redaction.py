"""记忆写入的脱敏收口 + 「不可信永不进全局」强制（M6 §8.9 / §12.2、§12.3）。

落盘的记忆文本先过这里逐叶脱敏（与会话、trace 同一招）。`MemoryStore` 的写入面只收
`RedactedMemoryEntry` / `RedactedMemorySections`——它们只由本模块铸造，第三方 store 绕不过脱敏。

铸造时还强制 §8.9 的承重不变量：**不可信内容不得进全局记忆**。全局记忆一旦污染会影响此后所有任务，
比没有记忆更糟，所以这条在铸造处就拒——而不是指望每个 store 实现自觉。
"""

from __future__ import annotations

from dataclasses import replace
from typing import final

from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ContractViolation
from syndica.kernel.memory_types import MemoryEntry, MemoryScope, MemorySection
from syndica.kernel.redaction import Redactor

__all__ = [
    'RedactedMemoryEntry',
    'RedactedMemorySections',
    'redacted_memory_entry',
    'redacted_memory_sections',
]

_MINT = object()


def _refuse_untrusted_global(scope: MemoryScope, trust: TrustLevel, what: str) -> None:
    if scope is MemoryScope.GLOBAL and trust is TrustLevel.UNTRUSTED:
        raise ContractViolation(
            expected='全局记忆只收可信内容（spec §8.9：不可信永不进全局）',
            received=f'{what}的 scope=GLOBAL 且 trust=UNTRUSTED',
            next_action='把它写进 WORKSPACE 层（按需注入），或先验证来源再以可信身份写全局',
        )


@final
class RedactedMemoryEntry:
    """`redacted_memory_entry` 的输出：脱敏过、且已校验不是「不可信进全局」的一条短期记忆。"""

    __slots__ = ('entry',)

    def __init__(self, entry: MemoryEntry, *, _mint: object = None) -> None:
        if _mint is not _MINT:
            raise ContractViolation(
                expected='RedactedMemoryEntry 只由 redacted_memory_entry 构造',
                received='有代码直接构造了它',
                next_action='把 entry 交给 redacted_memory_entry(entry, redactor)',
            )
        self.entry = entry


@final
class RedactedMemorySections:
    """`redacted_memory_sections` 的输出：脱敏过、且每段都过了「不可信进全局」校验的长期段落。"""

    __slots__ = ('scope', 'workspace', 'sections', 'through_seq')

    def __init__(
        self,
        scope: MemoryScope,
        workspace: str | None,
        sections: list[MemorySection],
        through_seq: int,
        *,
        _mint: object = None,
    ) -> None:
        if _mint is not _MINT:
            raise ContractViolation(
                expected='RedactedMemorySections 只由 redacted_memory_sections 构造',
                received='有代码直接构造了它',
                next_action='把段落交给 redacted_memory_sections(...)',
            )
        self.scope = scope
        self.workspace = workspace
        self.sections = sections
        self.through_seq = through_seq


def redacted_memory_entry(entry: MemoryEntry, redactor: Redactor) -> RedactedMemoryEntry:
    _refuse_untrusted_global(entry.scope, entry.trust, '这条短期记忆')
    redacted = replace(entry, text=redactor.redact(entry.text))
    return RedactedMemoryEntry(redacted, _mint=_MINT)


def redacted_memory_sections(
    scope: MemoryScope,
    workspace: str | None,
    sections: list[MemorySection],
    through_seq: int,
    redactor: Redactor,
) -> RedactedMemorySections:
    redacted: list[MemorySection] = []
    for section in sections:
        _refuse_untrusted_global(scope, section.trust, f'段落 {section.heading!r}')
        redacted.append(
            replace(
                section,
                heading=redactor.redact(section.heading),
                text=redactor.redact(section.text),
            )
        )
    return RedactedMemorySections(scope, workspace, redacted, through_seq, _mint=_MINT)
