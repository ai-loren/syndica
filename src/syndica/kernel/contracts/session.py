"""SessionStore 契约。任何实现都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方写 backend 时继承它并提供
`store` fixture 即可验证合规。

**运行前提**见 `syndica.kernel.contracts.base`：async 用例要有插件驱动。

三条读写纪律（spec §4.4）在这里钉成可执行用例：`exists` / `load_meta` 不取正文；归档序号只增不补；
按 id 的读写是 `load` / `load_meta` / `save` 的事，不经 `list_metas`。并发 `save` 取 last-writer，
最终必须是其中**某一个**完整的记录，不是两份的混合（基数取 3：n=1 看不出混合）。
"""

from __future__ import annotations

import asyncio

from syndica.kernel.context import ContextBlock
from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.errors import SyndicaError
from syndica.kernel.protocols.session import SessionStore
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.session_redaction import (
    RedactedContent,
    RedactedSessionRecord,
    redacted_content,
    redacted_record,
)
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus

__all__ = ['SessionStoreContract']

_REDACTOR = RuleRedactor()


def _record(sid: str = 's1', *, title: str = '', text: str = 'hello') -> SessionRecord:
    return SessionRecord(
        meta=SessionMeta(sid=sid, title=title, agent='Worker'),
        content=[ContextBlock(role='user', content=text)],
        system_prompt='SP',
        flags={'mode': 'dev'},
    )


def _red(record: SessionRecord) -> RedactedSessionRecord:
    return redacted_record(record, _REDACTOR)


def _redc(blocks: list[ContextBlock]) -> RedactedContent:
    return redacted_content(blocks, _REDACTOR)


class SessionStoreContract(ContractSuite):
    """子类必须提供一个 `store` fixture，且每个用例拿到的必须是空存储。"""

    async def test_save_then_load_roundtrips(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record(text='alpha')))
        loaded = await store.load('s1')
        assert loaded is not None
        assert loaded.meta.sid == 's1'
        assert [block.content for block in loaded.content] == ['alpha']
        assert loaded.system_prompt == 'SP'
        assert loaded.flags == {'mode': 'dev'}

    async def test_load_unknown_returns_none(self, store: SessionStore) -> None:
        assert await store.load('absent') is None
        assert await store.load_meta('absent') is None

    async def test_exists_distinguishes_present_and_absent(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record()))
        assert await store.exists('s1') is True
        assert await store.exists('absent') is False

    async def test_load_meta_returns_the_metadata(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record(title='A title')))
        meta = await store.load_meta('s1')
        assert meta is not None
        assert (meta.sid, meta.title, meta.agent) == ('s1', 'A title', 'Worker')

    async def test_save_rejects_a_sid_that_does_not_match_the_record(
        self, store: SessionStore
    ) -> None:
        try:
            await store.save('s1', _red(_record(sid='other')))
        except SyndicaError:
            pass
        else:
            raise AssertionError('save 应拒绝 meta.sid 与 sid 不一致的记录')
        assert await store.exists('s1') is False
        assert await store.exists('other') is False

    async def test_save_isolates_the_stored_record_from_the_caller(
        self, store: SessionStore
    ) -> None:
        record = _record(text='original')
        await store.save('s1', _red(record))
        record.content[0].content = 'mutated after save'
        record.flags['mode'] = 'mutated'
        loaded = await store.load('s1')
        assert loaded is not None
        assert loaded.content[0].content == 'original'
        assert loaded.flags == {'mode': 'dev'}

    async def test_load_hands_back_an_independent_copy(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record(text='original')))
        first = await store.load('s1')
        assert first is not None
        first.content[0].content = 'mutated the returned copy'
        second = await store.load('s1')
        assert second is not None
        assert second.content[0].content == 'original'

    async def test_archive_sequence_is_monotonic_and_does_not_backfill(
        self, store: SessionStore
    ) -> None:
        await store.save('s1', _red(_record()))
        first = await store.archive('s1', _red(_record(text='v1')))
        second = await store.archive('s1', _red(_record(text='v2')))
        third = await store.archive('s1', _red(_record(text='v3')))
        assert (first, second, third) == (1, 2, 3)
        meta = await store.load_meta('s1')
        assert meta is not None and meta.archived == 3

    async def test_concurrent_archive_gets_distinct_monotonic_sequences(
        self, store: SessionStore
    ) -> None:
        # 基数 3：读当前最大值与写新序号之间让出的实现会发同一个号，n=2 看不出。
        await store.save('s1', _red(_record()))
        seqs = await asyncio.gather(
            store.archive('s1', _red(_record(text='a'))),
            store.archive('s1', _red(_record(text='b'))),
            store.archive('s1', _red(_record(text='c'))),
        )
        assert sorted(seqs) == [1, 2, 3]

    async def test_read_archived_returns_the_snapshot(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record()))
        seq = await store.archive('s1', _red(_record(text='snap')))
        snapshot = await store.read_archived('s1', seq)
        assert snapshot is not None
        assert [block.content for block in snapshot.content] == ['snap']
        assert snapshot.meta.status is SessionStatus.ARCHIVED
        assert await store.read_archived('s1', 999) is None

    async def test_delete_removes_the_session_and_is_a_noop_when_absent(
        self, store: SessionStore
    ) -> None:
        await store.save('s1', _red(_record()))
        await store.delete('s1')
        assert await store.exists('s1') is False
        await store.delete('absent')

    async def test_list_metas_filters_by_the_closed_set(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record(sid='s1')))
        await store.save('s2', _red(_record(sid='s2')))
        all_metas = await store.list_metas()
        assert {meta.sid for meta in all_metas} == {'s1', 's2'}
        active = await store.list_metas(status=SessionStatus.ACTIVE)
        assert {meta.sid for meta in active} == {'s1', 's2'}
        assert {meta.sid for meta in await store.list_metas(agent='Worker')} == {'s1', 's2'}

    async def test_replace_content_replaces_only_content_and_bumps_updated_at(
        self, store: SessionStore
    ) -> None:
        record = _record(text='old')
        record.meta.updated_at = 0.0
        await store.save('s1', _red(record))
        await store.replace_content('s1', _redc([ContextBlock(role='user', content='new')]))
        loaded = await store.load('s1')
        assert loaded is not None
        assert [block.content for block in loaded.content] == ['new']
        assert loaded.meta.title == record.meta.title
        assert loaded.meta.updated_at > 0.0

    async def test_replace_content_rejects_an_unknown_session(self, store: SessionStore) -> None:
        try:
            await store.replace_content('absent', _redc([ContextBlock(role='user', content='x')]))
        except SyndicaError:
            pass
        else:
            raise AssertionError('replace_content 应拒绝不存在的会话')

    async def test_concurrent_saves_leave_one_whole_record_not_a_mix(
        self, store: SessionStore
    ) -> None:
        texts = ['alpha', 'bravo', 'charlie']
        await asyncio.gather(*(store.save('s1', _red(_record(text=text))) for text in texts))
        loaded = await store.load('s1')
        assert loaded is not None
        assert [block.content for block in loaded.content] in [[text] for text in texts]
