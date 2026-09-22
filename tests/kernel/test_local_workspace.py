"""本地工作区的路径监狱：越界在解析之后判定，相对根不查当前目录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.local_workspace import LocalWorkspace


def _workspace(root: Path) -> LocalWorkspace:
    return LocalWorkspace(str(root), shell='/bin/sh', env={})


def test_relative_root_is_rejected_without_calling_getcwd(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise AssertionError('getcwd')

    monkeypatch.setattr('os.getcwd', boom)
    with pytest.raises(ConfigError) as caught:
        LocalWorkspace('relative-dir', shell='/bin/sh', env={})
    assert '传入绝对路径' in caught.value.next_action


async def test_symlink_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    secret = tmp_path / 'secret.txt'
    secret.write_text('secret', encoding='utf-8')
    (root / 'link.txt').symlink_to(secret)
    workspace = _workspace(root)
    with pytest.raises(ConfigError):
        await workspace.read_text('link.txt', offset=1, limit=10)
    with pytest.raises(ConfigError):
        await workspace.write_text('link.txt', 'pwned')
    assert secret.read_text(encoding='utf-8') == 'secret'


async def test_sibling_prefix_is_not_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    sibling = tmp_path / 'ws-evil'
    sibling.mkdir()
    secret = sibling / 'secret.txt'
    secret.write_text('nope', encoding='utf-8')
    workspace = _workspace(root)
    with pytest.raises(ConfigError):
        await workspace.read_text(str(secret), offset=1, limit=10)


async def test_dotdot_is_rejected_after_resolve(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    (root / 'sub').mkdir(parents=True)
    secret = tmp_path / 'secret.txt'
    secret.write_text('nope', encoding='utf-8')
    workspace = _workspace(root)
    with pytest.raises(ConfigError):
        await workspace.read_text('sub/../../secret.txt', offset=1, limit=10)


async def test_write_then_read_roundtrips_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = _workspace(root)
    await workspace.write_text('notes/a.txt', 'hello')
    assert await workspace.read_text('notes/a.txt', offset=1, limit=10) == 'hello'
    assert (root / 'notes' / 'a.txt').read_text(encoding='utf-8') == 'hello'


def test_shell_must_be_an_executable_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    script = tmp_path / 'not-a-shell'
    script.write_text('nope', encoding='utf-8')
    script.chmod(0o644)
    with pytest.raises(ConfigError) as relative:
        LocalWorkspace(str(root), shell='sh', env={})
    assert '传入绝对路径' in relative.value.next_action
    with pytest.raises(ConfigError):
        LocalWorkspace(str(root), shell=str(script), env={})


def test_caller_env_mutation_does_not_change_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    env = {'A': '1'}
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env=env)
    env['A'] = '2'
    assert workspace._env == {'A': '1'}
    assert workspace._shell == '/bin/sh'


def test_source_does_not_read_process_cwd_or_environ() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ('local_workspace.py', 'workspace.py'):
        source = (root / 'src' / 'syndica' / 'kernel' / name).read_text(encoding='utf-8')
        assert 'getcwd' not in source
        assert 'environ' not in source


async def test_absolute_path_inside_the_root_roundtrips(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = _workspace(root)
    target = root / 'b.txt'
    await workspace.write_text(str(target), 'abs')
    assert await workspace.read_text(str(target), offset=1, limit=5) == 'abs'


async def test_close_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = _workspace(root)
    await workspace.close()
    await workspace.close()


async def test_exec_methods_run_poll_and_kill_inside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = _workspace(root)
    foreground = await workspace.run('printf direct', timeout=1, cwd='.')
    assert foreground.stdout == 'direct'
    assert foreground.running is False

    handle = await workspace.start('sleep 5', cwd='.')
    assert (await workspace.poll(handle)).running is True
    await workspace.kill(handle)
    assert (await workspace.poll(handle)).running is False
    await workspace.close()
