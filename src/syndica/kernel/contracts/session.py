"""SessionStore 契约。任何实现都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方写 backend 时继承它、提供
`store` fixture 即可验证合规。契约随 wheel 发布后再加断言就是破坏性变更，所以 round-trip 在这里一次
钉全：trust（两个取值，默认 TRUSTED 会让丢字段的实现 fail-open）、`extra`（键序进请求前缀，`==` 对
键序无感，另断 `list(extra)`）、tool_calls / ids、artifacts、每个 meta / record 字段。

**运行前提**见 `syndica.kernel.contracts.base`：async 用例要有插件驱动。

三条读写纪律（spec §4.4）钉成用例：`exists` / `load_meta` 不取正文；归档序号只增不补；按 id 的读写走
`load` / `load_meta` / `save`，不经 `list_metas`。并发 `save` 取 last-writer，最终是其中某一个完整记
录、不是两份的混合（基数取 3：n=1 看不出混合，且每条记录逐字段都不同）。
"""

from __future__ import annotations

import asyncio

from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall, TrustLevel
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
# 插入序刻意非字典序、也非其反转：键序进请求前缀（spec §5.2）。字典序是 message_id < syndica.key <
# zzz；插入序 zzz / message_id / syndica.key 两头都不是。syndica.key 的值还证明幂等键不被当凭证改。
_EXTRA = {'zzz': 'z', 'message_id': 'm1', 'syndica.key': 'env:isolation:0'}
# 三个工具，顺序既非升序（Bash/Read/Write）亦非降序，反转后仍两者都不是。
_TOOLS = ['Read', 'Write', 'Bash']
_FLAGS: dict[str, bool | int | str] = {'mode': 'dev', 'n': 2, 'on': True}


def _record(sid: str = 's1', *, title: str = '', text: str = 'hello') -> SessionRecord:
    """简单记录：只需要 sid / title / 一段文本的用例用它。"""
    return SessionRecord(
        meta=SessionMeta(sid=sid, title=title, agent='Worker'),
        content=[ContextBlock(role='user', content=text)],
        system_prompt='SP',
        flags={'mode': 'dev'},
    )


def _rich(
    sid: str = 's1',
    *,
    title: str = 'A title',
    text: str = 'hello',
    agent: str = 'Worker',
    workspace: str = '/w',
    status: SessionStatus = SessionStatus.ACTIVE,
    model: str = 'gpt',
    flags: dict[str, bool | int | str] | None = None,
    updated_at: float = 200.0,
) -> SessionRecord:
    """每个字段都非默认、含两种 trust、多模态内容、两个工具调用、artifacts、非字典序 extra 的记录。
    文本刻意不含密钥样式，脱敏是不动点，所以 round-trip 后逐字段相等。"""
    return SessionRecord(
        meta=SessionMeta(
            sid=sid,
            title=title,
            agent=agent,
            workspace=workspace,
            status=status,
            turns=4,
            archived=0,
            last_trace='trace-1',
            created_at=100.0,
            updated_at=updated_at,
        ),
        content=[
            ContextBlock(role='user', content=text, trust=TrustLevel.UNTRUSTED),
            ContextBlock(
                role='assistant',
                content=['part', {'type': 'text', 'text': 'more'}],
                tool_calls=[
                    ToolCall(id='c1', name='Bash', arguments='{"cmd": "ls"}'),
                    ToolCall(id='c2', name='Read', arguments='{"path": "a.py"}'),
                ],
                artifacts=[
                    ArtifactRef(
                        id='file:a',
                        path='a.py',
                        kind='file',
                        mime='text/x-python',
                        version='sha256:1',
                        producer=agent,
                        is_deliverable=True,
                    )
                ],
                trust=TrustLevel.TRUSTED,
                extra=dict(_EXTRA),
            ),
            ContextBlock(role='tool', content='out', tool_call_id='c1', trust=TrustLevel.TRUSTED),
        ],
        system_prompt='SP',
        model=model,
        tools=list(_TOOLS),
        flags=dict(_FLAGS) if flags is None else flags,
    )


def _red(record: SessionRecord) -> RedactedSessionRecord:
    return redacted_record(record, _REDACTOR)


def _redc(blocks: list[ContextBlock]) -> RedactedContent:
    return redacted_content(blocks, _REDACTOR)


class SessionStoreContract(ContractSuite):
    """子类必须提供一个 `store` fixture，且每个用例拿到的必须是空存储。"""

    async def test_save_then_load_roundtrips_every_field(self, store: SessionStore) -> None:
        await store.save('s1', _red(_rich('s1')))
        loaded = await store.load('s1')
        assert loaded is not None
        meta = loaded.meta
        assert (meta.sid, meta.title, meta.agent, meta.workspace, meta.status) == (
            's1',
            'A title',
            'Worker',
            '/w',
            SessionStatus.ACTIVE,
        )
        assert (meta.turns, meta.archived, meta.last_trace) == (4, 0, 'trace-1')
        assert (meta.created_at, meta.updated_at) == (100.0, 200.0)
        assert loaded.system_prompt == 'SP'
        assert loaded.model == 'gpt'
        assert loaded.tools == _TOOLS
        assert loaded.flags == _FLAGS
        assert [block.role for block in loaded.content] == ['user', 'assistant', 'tool']
        # 两个取值都在：一个「总是 TRUSTED」的 fail-open 实现会把第一个块洗成 TRUSTED。
        assert [block.trust for block in loaded.content] == [
            TrustLevel.UNTRUSTED,
            TrustLevel.TRUSTED,
            TrustLevel.TRUSTED,
        ]
        assert loaded.content[0].content == 'hello'
        assert loaded.content[1].content == ['part', {'type': 'text', 'text': 'more'}]
        assert [(c.id, c.name, c.arguments) for c in loaded.content[1].tool_calls] == [
            ('c1', 'Bash', '{"cmd": "ls"}'),
            ('c2', 'Read', '{"path": "a.py"}'),
        ]
        artifact = loaded.content[1].artifacts[0]
        assert (artifact.id, artifact.path, artifact.version) == ('file:a', 'a.py', 'sha256:1')
        assert artifact.is_deliverable is True
        assert loaded.content[2].tool_call_id == 'c1'
        # `==` 对键序无感；键序进请求前缀，另断 list(extra)。幂等键值不被当凭证改。
        assert loaded.content[1].extra == _EXTRA
        assert list(loaded.content[1].extra) == list(_EXTRA)

    async def test_load_unknown_returns_none(self, store: SessionStore) -> None:
        assert await store.load('absent') is None
        assert await store.load_meta('absent') is None

    async def test_exists_distinguishes_present_and_absent(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record()))
        assert await store.exists('s1') is True
        assert await store.exists('absent') is False

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

    async def test_archive_rejects_a_sid_that_does_not_match_the_record(
        self, store: SessionStore
    ) -> None:
        await store.save('s1', _red(_record(sid='s1')))
        try:
            await store.archive('s1', _red(_record(sid='other')))
        except SyndicaError:
            pass
        else:
            raise AssertionError('archive 应拒绝 meta.sid 与 sid 不一致的记录')

    async def test_save_keeps_its_own_copy_not_the_wrappers(self, store: SessionStore) -> None:
        # 隔离要跨「同一对象的两次读取」证明：`_red` 已深拷，改 record 碰不到 store；
        # 改 wrapper 里那份才证明 store 自己留了一份、没交出内部引用（F10）。
        wrapped = _red(_record(text='original'))
        await store.save('s1', wrapped)
        wrapped.record.content[0].content = 'mutated'
        wrapped.record.flags['mode'] = 'mutated'
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

    async def test_load_meta_hands_back_an_independent_copy(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record(title='A title')))
        first = await store.load_meta('s1')
        assert first is not None
        first.title = 'mutated the returned meta'
        second = await store.load_meta('s1')
        assert second is not None
        assert second.title == 'A title'

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
        seq = await store.archive('s1', _red(_rich('s1', text='snap')))
        snapshot = await store.read_archived('s1', seq)
        assert snapshot is not None
        assert snapshot.content[0].content == 'snap'
        assert snapshot.content[0].trust is TrustLevel.UNTRUSTED
        assert snapshot.meta.status is SessionStatus.ARCHIVED
        assert await store.read_archived('s1', 999) is None

    async def test_delete_removes_the_session_and_its_archives(self, store: SessionStore) -> None:
        await store.save('s1', _red(_record()))
        seq = await store.archive('s1', _red(_record(text='snap')))
        await store.delete('s1')
        assert await store.exists('s1') is False
        # 删会话要连历史副本一起删：留着 read_archived 还能取回已删会话的内容。
        assert await store.read_archived('s1', seq) is None
        await store.delete('absent')

    async def test_list_metas_filters_by_the_closed_set_and_orders_by_updated_at(
        self, store: SessionStore
    ) -> None:
        # 三个会话，agent / workspace / status / updated_at 各不同、插入序非 updated_at 序：每个过滤
        # 都要真的裁剪（都相同时「忽略过滤」也过），顺序按 updated_at 倒序精确断言（F10）。
        await store.save('s1', _red(_rich('s1', agent='Planner', workspace='/a', updated_at=300.0)))
        await store.save(
            's2',
            _red(
                _rich(
                    's2',
                    agent='Worker',
                    workspace='/b',
                    status=SessionStatus.ARCHIVED,
                    updated_at=100.0,
                )
            ),
        )
        await store.save('s3', _red(_rich('s3', agent='Worker', workspace='/a', updated_at=200.0)))
        assert [meta.sid for meta in await store.list_metas()] == ['s1', 's3', 's2']
        assert {m.sid for m in await store.list_metas(agent='Worker')} == {'s2', 's3'}
        assert {m.sid for m in await store.list_metas(workspace='/a')} == {'s1', 's3'}
        archived = await store.list_metas(status=SessionStatus.ARCHIVED)
        assert {m.sid for m in archived} == {'s2'}

    async def test_replace_content_replaces_only_content_and_keeps_all_other_fields(
        self, store: SessionStore
    ) -> None:
        record = _rich('s1', updated_at=0.0)
        await store.save('s1', _red(record))
        await store.replace_content('s1', _redc([ContextBlock(role='user', content='new')]))
        loaded = await store.load('s1')
        assert loaded is not None
        assert [block.content for block in loaded.content] == ['new']
        assert loaded.meta.title == 'A title'
        assert loaded.meta.agent == 'Worker'
        assert loaded.meta.workspace == '/w'
        assert loaded.meta.archived == 0
        assert loaded.meta.created_at == 100.0
        assert loaded.system_prompt == 'SP'
        assert loaded.model == 'gpt'
        assert loaded.flags == _FLAGS
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
        # 每条记录逐字段都不同：只差 content 时，一个分两步写 content 与 meta 的实现（混合）看不出。
        variants = [
            _rich('s1', title=f't{i}', text=f'x{i}', model=f'm{i}', flags={'k': i})
            for i in range(3)
        ]
        await asyncio.gather(*(store.save('s1', _red(v)) for v in variants))
        loaded = await store.load('s1')
        assert loaded is not None
        assert any(
            loaded.meta.title == v.meta.title
            and loaded.content[0].content == v.content[0].content
            and loaded.model == v.model
            and loaded.flags == v.flags
            for v in variants
        )
