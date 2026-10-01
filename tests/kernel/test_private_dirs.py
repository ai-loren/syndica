"""宿主留给自己的目录（M3 D17）：文件工具碰不到，命令不受影响。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tools.local import local_tools

_PRIVATE = ('.syndica/traces', '.syndica/handoffs')
_TRACE = '.syndica/traces/t1/spans.jsonl'
_HANDOFF = '.syndica/handoffs/h.md'


def _root(tmp_path: Path) -> Path:
    root = tmp_path / 'ws'
    for relative, text in (
        (_TRACE, 'trace line\n'),
        (_HANDOFF, 'handoff line\n'),
        ('.syndica/tmp/out.txt', 'tmp line\n'),
        ('.syndica/other/x.txt', 'other line\n'),
        ('.syndica/traces-old/x.txt', 'old line\n'),
        ('src/a.py', 'source line\n'),
        ('src/traces/x.txt', 'nested traces line\n'),
        ('docs/handoffs/y.md', 'nested handoffs line\n'),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return root


def _workspace(root: Path) -> LocalWorkspace:
    return LocalWorkspace(str(root), shell='/bin/sh', env={}, private=_PRIVATE)


@pytest.mark.parametrize(
    'path',
    [
        _TRACE,
        _HANDOFF,
        '.SYNDICA/Traces/t1/spans.jsonl',
        '.syndica/HANDOFFS/h.md',
        'src/../.syndica/traces/t1/spans.jsonl',
        './.syndica/handoffs/h.md',
        'linked/t1/spans.jsonl',
    ],
    ids=['trace', 'handoff', 'case', 'case-handoffs', 'parent-step', 'dot-slash', 'symlink'],
)
async def test_reading_inside_a_private_directory_is_refused(tmp_path: Path, path: str):
    root = _root(tmp_path)
    (root / 'linked').symlink_to(root / '.syndica' / 'traces', target_is_directory=True)

    with pytest.raises(ConfigError) as caught:
        await _workspace(root).read_text(path, offset=1, limit=10)

    assert path in caught.value.received
    assert '可信' in caught.value.next_action


def _handoffs_elsewhere(tmp_path: Path) -> Path:
    """`.syndica/handoffs` 是指向根内别处的符号链接：解析之后的路径不在私有目录里，只有名字在。"""
    root = _root(tmp_path)
    elsewhere = root / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'h.md').write_text('moved line\n', encoding='utf-8')
    handoffs = root / '.syndica' / 'handoffs'
    (handoffs / 'h.md').unlink()
    handoffs.rmdir()
    handoffs.symlink_to(elsewhere, target_is_directory=True)
    return root


@pytest.mark.parametrize(
    'path',
    [_HANDOFF, 'src/../.syndica/handoffs/h.md', '.SYNDICA/Handoffs/h.md', 'alias'],
    ids=['as-named', 'parent-step', 'case', 'root-alias'],
)
async def test_a_private_directory_that_is_a_symlink_is_refused_by_its_name(
    tmp_path: Path, path: str
):
    root = _handoffs_elsewhere(tmp_path)
    (tmp_path / 'alias').symlink_to(root, target_is_directory=True)
    if path == 'alias':
        path = str(tmp_path / 'alias' / _HANDOFF)

    with pytest.raises(ConfigError):
        await _workspace(root).read_text(path, offset=1, limit=10)


async def test_writing_through_a_private_directory_that_is_a_symlink_is_refused(tmp_path: Path):
    root = _handoffs_elsewhere(tmp_path)
    workspace = _workspace(root)

    with pytest.raises(ConfigError):
        await workspace.write_text('.syndica/handoffs/new.md', 'forged\n')
    with pytest.raises(ConfigError):
        await workspace.edit_text(_HANDOFF, 'moved', 'forged')

    assert not (root / 'elsewhere' / 'new.md').exists()
    assert (root / 'elsewhere' / 'h.md').read_text(encoding='utf-8') == 'moved line\n'


async def test_search_through_a_private_directory_that_is_a_symlink_finds_nothing(
    tmp_path: Path,
):
    workspace = _workspace(_handoffs_elsewhere(tmp_path))

    with pytest.raises(ConfigError):
        await workspace.grep_text('line', '.syndica/handoffs')
    assert '.syndica/handoffs' not in ' '.join(await workspace.glob_paths('.syndica/**/*'))


async def test_a_path_through_a_symlinked_git_directory_is_still_refused(tmp_path: Path):
    """`.git` 指向真正的 git 目录里的一层时，`.git/../x` 解析之后落在 git 目录里，而折叠之后的名字
    里已经没有 `.git`：照写的样子才看得出它经过了谁。"""
    root = tmp_path / 'ws'
    (root / 'gitdir' / 'sub').mkdir(parents=True)
    (root / '.git').symlink_to(root / 'gitdir' / 'sub', target_is_directory=True)
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})

    with pytest.raises(ConfigError):
        await workspace.write_text('.git/../config', 'forged\n')
    assert not (root / 'gitdir' / 'config').exists()


async def test_a_file_cannot_take_the_place_of_a_private_directory(tmp_path: Path):
    """在 `.syndica` 的位置上建一个文件，宿主就建不出 `.syndica/traces`，trace 记录跟着失败。"""
    root = tmp_path / 'fresh'
    root.mkdir()
    workspace = _workspace(root)

    for path in ('.syndica', '.SYNDICA', './.syndica'):
        with pytest.raises(ConfigError) as caught:
            await workspace.write_text(path, 'blocked\n')
        assert '.syndica/traces' in caught.value.expected

    assert not (root / '.syndica').exists()
    await workspace.write_text('.syndica-notes', 'fine\n')
    assert (root / '.syndica-notes').read_text(encoding='utf-8') == 'fine\n'


async def test_an_absolute_path_inside_a_private_directory_is_refused(tmp_path: Path):
    root = _root(tmp_path)

    with pytest.raises(ConfigError):
        await _workspace(root).read_text(str(root / _TRACE), offset=1, limit=10)


async def test_writing_and_editing_inside_a_private_directory_change_nothing(tmp_path: Path):
    root = _root(tmp_path)
    workspace = _workspace(root)

    with pytest.raises(ConfigError):
        await workspace.write_text(_HANDOFF, 'forged\n')
    with pytest.raises(ConfigError):
        await workspace.write_text('.syndica/traces/new.txt', 'forged\n')
    with pytest.raises(ConfigError):
        await workspace.edit_text(_TRACE, 'trace', 'forged')

    assert (root / _HANDOFF).read_text(encoding='utf-8') == 'handoff line\n'
    assert (root / _TRACE).read_text(encoding='utf-8') == 'trace line\n'
    assert not (root / '.syndica' / 'traces' / 'new.txt').exists()


@pytest.mark.parametrize(
    ('path', 'text'),
    [
        ('.syndica/tmp/out.txt', 'tmp line'),
        ('.syndica/other/x.txt', 'other line'),
        ('.syndica/traces-old/x.txt', 'old line'),
        ('src/traces/x.txt', 'nested traces line'),
        ('docs/handoffs/y.md', 'nested handoffs line'),
    ],
    ids=[
        'tmp',
        'another-directory',
        'a-longer-sibling-name',
        'same-name-deeper-down',
        'same-name-elsewhere',
    ],
)
async def test_the_rest_of_the_hosts_directory_stays_usable(tmp_path: Path, path: str, text: str):
    """只关点名的目录：本仓的 Worker 把长输出存进 `.syndica/tmp/` 再读。"""
    root = _root(tmp_path)
    workspace = _workspace(root)

    assert await workspace.read_text(path, offset=1, limit=10) == text
    await workspace.write_text(path, 'rewritten\n')
    assert (root / path).read_text(encoding='utf-8') == 'rewritten\n'


@pytest.mark.parametrize('pattern', ['.syndica/traces/**/*', '.SYNDICA/handoffs/*.md'])
async def test_a_glob_aimed_at_a_private_directory_is_refused(tmp_path: Path, pattern: str):
    with pytest.raises(ConfigError) as caught:
        await _workspace(_root(tmp_path)).glob_paths(pattern)

    assert '可信' in caught.value.next_action


async def test_a_glob_over_the_hosts_directory_leaves_the_private_ones_out(tmp_path: Path):
    root = _root(tmp_path)
    (root / 'linked').symlink_to(root / '.syndica' / 'traces', target_is_directory=True)
    workspace = _workspace(root)

    assert await workspace.glob_paths('.syndica/**/*') == (
        '.syndica/other',
        '.syndica/other/x.txt',
        '.syndica/tmp',
        '.syndica/tmp/out.txt',
        '.syndica/traces-old',
        '.syndica/traces-old/x.txt',
    )
    assert await workspace.glob_paths('linked/*') == ()


@pytest.mark.parametrize('path', ['.syndica/handoffs', '.syndica/traces/t1', 'linked'])
async def test_a_grep_aimed_at_a_private_directory_is_refused(tmp_path: Path, path: str):
    root = _root(tmp_path)
    (root / 'linked').symlink_to(root / '.syndica' / 'traces', target_is_directory=True)

    with pytest.raises(ConfigError):
        await _workspace(root).grep_text('line', path)


async def test_a_grep_over_the_hosts_directory_leaves_the_private_ones_out(tmp_path: Path):
    hits = await _workspace(_root(tmp_path)).grep_text('line', '.syndica')

    assert hits == (
        '.syndica/other/x.txt:1:other line',
        '.syndica/tmp/out.txt:1:tmp line',
        '.syndica/traces-old/x.txt:1:old line',
    )


async def test_commands_still_reach_the_private_directories(tmp_path: Path):
    """命令的输出本来就不可信；真正的边界是沙箱。"""
    workspace = _workspace(_root(tmp_path))
    try:
        result = await workspace.run(f'cat {_TRACE}', timeout=10, cwd='.')
    finally:
        await workspace.close()

    assert result.stdout == 'trace line\n'


async def test_the_read_tool_tells_the_model_why(tmp_path: Path):
    workspace = _workspace(_root(tmp_path))
    read = local_tools(workspace.portal())['Read']

    result = await read.execute(path=_HANDOFF)

    assert 'handoff line' not in result.output
    assert '.syndica/traces、.syndica/handoffs' in result.output
    assert '可信' in result.output


async def test_without_private_directories_every_path_stays_reachable(tmp_path: Path):
    root = _root(tmp_path)
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})

    assert await workspace.read_text(_TRACE, offset=1, limit=10) == 'trace line'


@pytest.mark.parametrize(
    ('private', 'anchor'),
    [
        ('.syndica/traces', '逐字符'),
        (b'.syndica/traces', '逐字符'),
        (None, '逐字符'),
        (['/abs/dir'], '相对'),
        (['../outside'], '..'),
        (['.'], '根本身'),
        ([''], '根本身'),
        ([7], '相对'),
    ],
    ids=['a-string', 'bytes', 'none', 'absolute', 'parent', 'root', 'empty', 'not-a-string'],
)
def test_private_directories_must_be_relative_directories(
    tmp_path: Path, private: object, anchor: str
):
    with pytest.raises(ConfigError) as caught:
        LocalWorkspace(str(tmp_path), shell='/bin/sh', env={}, private=private)  # pyright: ignore[reportArgumentType]

    assert anchor in f'{caught.value.expected} {caught.value.next_action}'
