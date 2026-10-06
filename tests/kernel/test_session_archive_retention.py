"""内置会话 store 只留最近 20 份压缩前像：跑几天的会话每压缩一次就归档一份完整轨迹。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.kernel.context import ContextBlock
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.session_redaction import redacted_record
from syndica.kernel.session_types import SessionMeta, SessionRecord
from syndica.kernel.stores.file_session import FileSessionStore
from syndica.kernel.stores.memory_session import InMemorySessionStore


def _record(text: str) -> SessionRecord:
    return SessionRecord(
        meta=SessionMeta(sid='s1', agent='Worker'),
        content=[ContextBlock(role='user', content=text)],
        system_prompt='SP',
        flags={},
    )


@pytest.fixture(params=['memory', 'file'])
def store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> InMemorySessionStore | FileSessionStore:
    if request.param == 'memory':
        return InMemorySessionStore()
    return FileSessionStore(tmp_path / 'sessions')


async def test_only_the_newest_twenty_archives_are_kept(
    store: InMemorySessionStore | FileSessionStore,
) -> None:
    redactor = RuleRedactor()
    seqs = [
        await store.archive('s1', redacted_record(_record(f'v{i}'), redactor)) for i in range(23)
    ]
    assert seqs == list(range(1, 24))
    assert [await store.read_archived('s1', seq) is None for seq in (1, 2, 3)] == [True] * 3
    oldest_kept = await store.read_archived('s1', 4)
    newest = await store.read_archived('s1', 23)
    assert oldest_kept is not None and oldest_kept.content[0].content == 'v3'
    assert newest is not None and newest.content[0].content == 'v22'


async def test_pruning_does_not_reuse_sequence_numbers(
    store: InMemorySessionStore | FileSessionStore,
) -> None:
    redactor = RuleRedactor()
    for i in range(21):
        await store.archive('s1', redacted_record(_record(f'v{i}'), redactor))
    assert await store.archive('s1', redacted_record(_record('next'), redactor)) == 22
