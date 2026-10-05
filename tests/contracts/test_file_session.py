from pathlib import Path

import pytest

from syndica.kernel.context import ContextBlock
from syndica.kernel.contracts.session import SessionStoreContract
from syndica.kernel.protocols.session import SessionStore
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.session_redaction import (
    RedactedSessionRecord,
    redacted_content,
    redacted_record,
)
from syndica.kernel.session_types import SessionMeta, SessionRecord
from syndica.kernel.stores.file_session import FileSessionStore


class TestFileSessionStore(SessionStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> SessionStore:
        return FileSessionStore(tmp_path / '.syndica' / 'sessions')


def _red(sid: str, content: list[ContextBlock]) -> RedactedSessionRecord:
    record = SessionRecord(meta=SessionMeta(sid=sid), content=content)
    return redacted_record(record, RuleRedactor())


async def test_extra_key_order_and_idempotency_key_survive_round_trip(tmp_path: Path) -> None:
    # F5：文件 store 不得重脱 / 排序结构字段。extra 键序进请求前缀；内核幂等键 syndica.key 的值
    # 被当凭证改掉会让恢复后按键查重失效、环境事实重复注入。插入序刻意非字典序。
    store = FileSessionStore(tmp_path / 'sessions')
    extra = {'zzz': '1', 'aaa': '2', 'syndica.key': 'env:isolation:0xDEADBEEFcafef00dbaad1234'}
    block = ContextBlock(role='user', content='hi', extra=dict(extra))
    await store.save('s', _red('s', [block]))
    loaded = await store.load('s')
    assert loaded is not None
    assert list(loaded.content[0].extra) == list(extra)
    assert loaded.content[0].extra['syndica.key'] == extra['syndica.key']


async def test_half_made_session_dir_is_absent_and_resave_works(tmp_path: Path) -> None:
    # F2：首存在写 record 前失败只留一个空目录。按目录判存在会让 exists 说有、load 取不回，并让此后
    # 每次存盘先在 load_meta 上炸、永远存不进去。按最后写的 meta 判存在，半截目录即「不存在」。
    root = tmp_path / 'sessions'
    store = FileSessionStore(root)
    (root / 's').mkdir(parents=True)
    assert await store.exists('s') is False
    assert await store.load_meta('s') is None
    await store.save('s', _red('s', [ContextBlock(role='user', content='hi')]))
    assert await store.exists('s') is True
    assert await store.load_meta('s') is not None


async def test_archive_count_survives_replace_content(tmp_path: Path) -> None:
    # F6：meta 两份副本（meta.json 与 record.json 内嵌）。archive 只前移 meta.json，replace_content
    # 从 record.json 读会把 archived 回滚。load 与 load_meta 的 archived 要一致、且 replace 不回滚。
    store = FileSessionStore(tmp_path / 'sessions')
    red = _red('s', [ContextBlock(role='user', content='hi')])
    await store.save('s', red)
    assert await store.archive('s', red) == 1
    meta = await store.load_meta('s')
    loaded = await store.load('s')
    assert meta is not None and loaded is not None
    assert meta.archived == 1 and loaded.meta.archived == 1
    await store.replace_content(
        's', redacted_content([ContextBlock(role='user', content='new')], RuleRedactor())
    )
    after_meta = await store.load_meta('s')
    after_load = await store.load('s')
    assert after_meta is not None and after_load is not None
    assert after_meta.archived == 1 and after_load.meta.archived == 1
