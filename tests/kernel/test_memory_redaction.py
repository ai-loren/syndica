"""记忆脱敏收口 + 「不可信永不进全局」强制（M6 §8.9 / §12.2、§12.3）。"""

from __future__ import annotations

import pytest

from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ContractViolation
from syndica.kernel.memory_redaction import (
    RedactedMemoryEntry,
    RedactedMemorySections,
    redacted_memory_entry,
    redacted_memory_sections,
)
from syndica.kernel.memory_types import MemoryEntry, MemoryScope, MemorySection
from syndica.kernel.redaction import RuleRedactor

_REDACTOR = RuleRedactor()


def _entry(scope: MemoryScope, trust: TrustLevel, text: str = 'note') -> MemoryEntry:
    workspace = None if scope is MemoryScope.GLOBAL else '/w'
    return MemoryEntry(seq=0, scope=scope, workspace=workspace, text=text, trust=trust)


def test_untrusted_cannot_enter_global_memory() -> None:
    with pytest.raises(ContractViolation, match='全局'):
        redacted_memory_entry(_entry(MemoryScope.GLOBAL, TrustLevel.UNTRUSTED), _REDACTOR)


def test_trusted_global_memory_is_allowed() -> None:
    minted = redacted_memory_entry(_entry(MemoryScope.GLOBAL, TrustLevel.TRUSTED), _REDACTOR)
    assert minted.entry.scope is MemoryScope.GLOBAL


def test_untrusted_is_allowed_in_workspace_memory() -> None:
    minted = redacted_memory_entry(_entry(MemoryScope.WORKSPACE, TrustLevel.UNTRUSTED), _REDACTOR)
    assert minted.entry.trust is TrustLevel.UNTRUSTED


def test_an_untrusted_section_cannot_enter_global() -> None:
    with pytest.raises(ContractViolation, match='全局'):
        redacted_memory_sections(
            MemoryScope.GLOBAL,
            None,
            [MemorySection(heading='h', text='t', trust=TrustLevel.UNTRUSTED)],
            0,
            _REDACTOR,
        )


def test_the_redacted_types_reject_direct_construction() -> None:
    with pytest.raises(ContractViolation, match='只由'):
        RedactedMemoryEntry(_entry(MemoryScope.WORKSPACE, TrustLevel.TRUSTED))
    with pytest.raises(ContractViolation, match='只由'):
        RedactedMemorySections(MemoryScope.WORKSPACE, '/w', [], 0)


def test_redaction_scrubs_secrets_in_entry_and_sections() -> None:
    redactor = RuleRedactor(secrets=['sk-abc123'])
    entry = redacted_memory_entry(
        MemoryEntry(
            seq=0,
            scope=MemoryScope.WORKSPACE,
            workspace='/w',
            text='key is sk-abc123 ok',
            trust=TrustLevel.TRUSTED,
        ),
        redactor,
    )
    assert 'sk-abc123' not in str(entry.entry.text)
    sections = redacted_memory_sections(
        MemoryScope.WORKSPACE,
        '/w',
        [MemorySection(heading='h', text='token sk-abc123 here')],
        0,
        redactor,
    )
    assert 'sk-abc123' not in sections.sections[0].text
