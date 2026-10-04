"""版本层（M5 D4、D5）：Python 写的 git 格式对象，脱敏之后落盘，从不调用 git。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

import syndica.kernel.versions as versions_module
from syndica.kernel.errors import ConfigError
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.versions import VersionStore

_SECRET = 'quiz-secret-value-9'


def _digest(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


@pytest.fixture
def store(tmp_path: Path) -> VersionStore:
    return VersionStore(tmp_path, redactor=RuleRedactor(secrets=[_SECRET]))


def _stored_bytes(store: VersionStore) -> list[bytes]:
    return [
        zlib.decompress(path.read_bytes())
        for path in (store.location / 'objects').rglob('*')
        if path.is_file()
    ]


def test_a_recorded_version_reads_back_byte_for_byte(store: VersionStore) -> None:
    text = store.record('notes.md', b'# title\n', producer='alpha')
    binary = store.record('blob.bin', b'\x00\xff\xfe', producer='alpha')

    assert text is not None and binary is not None
    assert store.content('notes.md', _digest(b'# title\n')) == b'# title\n'
    assert store.content('blob.bin', _digest(b'\x00\xff\xfe')) == b'\x00\xff\xfe'
    assert (text.version, binary.version) == (_digest(b'# title\n'), _digest(b'\x00\xff\xfe'))


def test_history_lists_each_change_newest_first_with_its_producer(store: VersionStore) -> None:
    for producer, content in (('charlie', b'one'), ('alpha', b'two'), ('bravo', b'three')):
        store.record('notes.md', content, producer=producer)

    history = store.history('notes.md')

    assert [(entry.producer, entry.version) for entry in history] == [
        ('bravo', _digest(b'three')),
        ('alpha', _digest(b'two')),
        ('charlie', _digest(b'one')),
    ]


def test_rewriting_identical_content_adds_no_history_entry(store: VersionStore) -> None:
    store.record('notes.md', b'same', producer='alpha')
    store.record('notes.md', b'same', producer='bravo')

    assert [entry.producer for entry in store.history('notes.md')] == ['alpha']


def test_a_removal_shows_up_in_history_and_a_later_write_comes_back(store: VersionStore) -> None:
    store.record('notes.md', b'first', producer='alpha')
    removed = store.record_removal('notes.md', producer='bravo')
    store.record('notes.md', b'again', producer='charlie')

    history = store.history('notes.md')

    assert removed is not None and removed.removed
    assert [(entry.producer, entry.removed, entry.version) for entry in history] == [
        ('charlie', False, _digest(b'again')),
        ('bravo', True, None),
        ('alpha', False, _digest(b'first')),
    ]


def test_removing_a_file_the_store_never_saw_records_nothing(store: VersionStore) -> None:
    assert store.record_removal('never.md', producer='alpha') is None
    assert store.history('never.md') == []


def test_nested_paths_and_siblings_keep_their_own_history(store: VersionStore) -> None:
    store.record('docs/a/one.md', b'one', producer='alpha')
    store.record('docs/b.md', b'b', producer='bravo')
    store.record('docs/a/one.md', b'uno', producer='charlie')
    store.record_removal('docs/b.md', producer='delta')

    assert [entry.producer for entry in store.history('docs/a/one.md')] == ['charlie', 'alpha']
    assert [entry.producer for entry in store.history('docs/b.md')] == ['delta', 'bravo']


def test_absolute_and_dotted_paths_name_the_same_file(store: VersionStore, tmp_path: Path) -> None:
    store.record(str(tmp_path / 'docs' / 'x.md'), b'one', producer='alpha')
    store.record('./docs/../docs/x.md', b'two', producer='bravo')

    assert [entry.producer for entry in store.history('docs/x.md')] == ['bravo', 'alpha']


@pytest.mark.parametrize('path', ['../outside.md', '/etc/passwd', '.', ''])
def test_a_path_outside_the_root_is_refused(store: VersionStore, path: str) -> None:
    with pytest.raises(ConfigError) as refused:
        store.record(path, b'x', producer='alpha')

    assert '相对工作区根' in refused.value.next_action


def test_diff_is_a_unified_diff_for_text_and_a_size_note_for_binary(store: VersionStore) -> None:
    store.record('notes.md', b'alpha\nbravo\n', producer='alpha')
    store.record('notes.md', b'alpha\ncharlie\n', producer='alpha')
    store.record('blob.bin', b'\xff', producer='alpha')
    store.record('blob.bin', b'\xff\xfe\xfd', producer='alpha')

    text = store.diff('notes.md', _digest(b'alpha\nbravo\n'), _digest(b'alpha\ncharlie\n'))
    binary = store.diff('blob.bin', _digest(b'\xff'), _digest(b'\xff\xfe\xfd'))

    assert '-bravo\n' in text and '+charlie\n' in text
    assert '1 字节 → 3 字节' in binary


def test_an_unknown_version_says_how_to_find_the_recorded_ones(store: VersionStore) -> None:
    store.record('notes.md', b'one', producer='alpha')

    with pytest.raises(ConfigError) as refused:
        store.content('notes.md', _digest(b'never written'))

    assert 'history' in refused.value.next_action


def test_a_secret_is_redacted_before_it_reaches_disk_and_cannot_be_restored(
    store: VersionStore,
) -> None:
    secret_text = f'token = {_SECRET}\n'.encode()
    entry = store.record('config.py', secret_text, producer='alpha')

    assert entry is not None
    assert (entry.redacted, entry.version) == (True, None)
    assert all(_SECRET.encode() not in raw for raw in _stored_bytes(store))
    [listed] = store.history('config.py')
    assert (listed.redacted, listed.version) == (True, None)
    with pytest.raises(ConfigError) as refused:
        store.content('config.py', _digest(secret_text))
    assert '脱过敏' in refused.value.received


def test_a_secret_inside_binary_data_is_redacted_too(store: VersionStore) -> None:
    entry = store.record('blob.bin', b'\xff\xfe' + _SECRET.encode() + b'\x00', producer='alpha')

    assert entry is not None and entry.redacted
    assert all(_SECRET.encode() not in raw for raw in _stored_bytes(store))


def test_a_secret_in_the_producer_never_reaches_the_commit_message(store: VersionStore) -> None:
    store.record('notes.md', b'plain', producer=_SECRET)

    assert all(_SECRET.encode() not in raw for raw in _stored_bytes(store))


def test_a_path_that_looks_like_a_secret_is_not_recorded(
    store: VersionStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.versions'):
        entry = store.record(f'keys/{_SECRET}.txt', b'plain', producer='alpha')

    assert entry is None
    assert not (store.location / 'objects').exists()
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert _SECRET not in caplog.text


def test_a_file_over_the_size_cap_is_not_recorded_and_says_so(
    store: VersionStore, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(versions_module, '_MAX_RECORDED', 4)

    with caplog.at_level(logging.WARNING, logger='syndica.kernel.versions'):
        entry = store.record('big.bin', b'12345', producer='alpha')

    assert entry is None
    assert [record.args for record in caplog.records] == [('big.bin', 5, 4)]


def test_a_stale_lock_is_refused_with_how_to_clear_it(
    store: VersionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(versions_module, '_LOCK_ATTEMPTS', 2)
    lock = store.location / 'refs' / 'heads' / 'syndica.lock'
    lock.parent.mkdir(parents=True)
    lock.write_text('')

    with pytest.raises(ConfigError) as refused:
        store.record('notes.md', b'x', producer='alpha')

    assert '删掉它' in refused.value.next_action
    assert lock.exists()


def test_a_corrupted_object_says_to_drop_the_store(store: VersionStore) -> None:
    store.record('notes.md', b'x', producer='alpha')
    for path in (store.location / 'objects').rglob('*'):
        if path.is_file():
            path.write_bytes(b'not zlib')

    with pytest.raises(ConfigError) as refused:
        store.history('notes.md')

    assert '删掉整个目录' in refused.value.next_action


def test_no_operation_runs_a_process_or_reads_the_store_config(
    store: VersionStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / 'executed'
    store.record('seed.md', b'seed', producer='alpha')
    (store.location / 'config').write_text(
        f'[core]\n\tfsmonitor = touch {marker}\n'
        f'[filter "x"]\n\tclean = touch {marker}\n\tsmudge = touch {marker}\n'
        f'[include]\n\tpath = {tmp_path / "elsewhere"}\n'
    )
    (tmp_path / '.gitattributes').write_text('* filter=x\n')

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError('版本层起了一个进程')

    monkeypatch.setattr(subprocess, 'Popen', refuse)
    monkeypatch.setattr(os, 'posix_spawn', refuse)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', refuse)
    store.record('notes.md', b'one', producer='alpha')
    store.record('notes.md', b'two', producer='bravo')
    store.history('notes.md')
    store.diff('notes.md', _digest(b'one'), _digest(b'two'))
    store.content('notes.md', _digest(b'one'))
    store.record_removal('notes.md', producer='charlie')

    assert not marker.exists()


@pytest.mark.skipif(shutil.which('git') is None, reason='需要 git 来核对格式')
def test_git_reads_the_store_as_an_ordinary_repository(store: VersionStore, tmp_path: Path) -> None:
    store.record('docs/a.md', b'alpha\n', producer='alpha')
    store.record('b.txt', b'bravo\n', producer='bravo')
    store.record('docs/a.md', b'alpha two\n', producer='charlie')
    home = tmp_path / 'home'
    home.mkdir()
    environment = {
        'PATH': os.defpath,
        'HOME': str(home),
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
    }

    def git(*arguments: str) -> str:
        done = subprocess.run(
            ['git', f'--git-dir={store.location}', *arguments],
            capture_output=True,
            text=True,
            env=environment,
            cwd=home,
            check=True,
        )
        return done.stdout

    git('fsck', '--strict')
    assert git('show', 'HEAD:docs/a.md') == 'alpha two\n'
    assert git('show', 'HEAD~1:docs/a.md') == 'alpha\n'
    assert git('log', '--format=%s').split('\n')[:3] == [
        'charlie docs/a.md',
        'bravo b.txt',
        'alpha docs/a.md',
    ]
