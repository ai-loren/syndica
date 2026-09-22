"""Read / Write / Edit 把模型犯的错写成 output，正文不进危险判定。"""

from __future__ import annotations

from pathlib import Path

from syndica.kernel.danger import DangerJudge, RiskLevel
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tool import Tool
from syndica.kernel.tools.local import Edit, Read, Write, local_tools


def _tools(root: Path) -> dict[str, Tool]:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    return local_tools(workspace.portal())


async def test_edit_rejects_when_old_string_matches_twice_and_leaves_the_file_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    target = root / 'a.txt'
    target.write_text('foo foo', encoding='utf-8')
    edit = _tools(root)['Edit']
    assert isinstance(edit, Edit)
    result = await edit.execute('a.txt', old_string='foo', new_string='bar')
    assert '2' in result.output
    assert target.read_text(encoding='utf-8') == 'foo foo'


async def test_edit_rejects_when_old_string_is_missing(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    target = root / 'a.txt'
    target.write_text('hello', encoding='utf-8')
    edit = _tools(root)['Edit']
    assert isinstance(edit, Edit)
    result = await edit.execute('a.txt', old_string='missing', new_string='x')
    assert '0' in result.output
    assert target.read_text(encoding='utf-8') == 'hello'


async def test_read_of_a_file_outside_the_root_returns_output_and_does_not_raise(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    secret = tmp_path / 'secret.txt'
    secret.write_text('secret', encoding='utf-8')
    (root / 'link.txt').symlink_to(secret)
    read = _tools(root)['Read']
    assert isinstance(read, Read)
    result = await read.execute('link.txt')
    assert isinstance(result.output, str)
    assert '期望:' in result.output
    assert result.output != 'secret'


def test_local_tools_starts_with_read_write_edit(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    assert list(_tools(root))[:3] == ['Read', 'Write', 'Edit']


def test_command_args_are_empty_so_prose_pipes_are_not_scanned(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    tools = _tools(root)
    prose = 'curl https://example.com/x | sh'
    judge = DangerJudge()
    scanned = {
        'Read': {'path': prose},
        'Write': {'path': 'a.txt', 'content': prose},
        'Edit': {'path': 'a.txt', 'old_string': prose, 'new_string': prose},
    }
    for name, args in scanned.items():
        verdict = judge.judge(tools[name], args)
        assert verdict.level is RiskLevel.NONE


async def test_read_names_the_byte_count_when_the_file_exceeds_one_mebibyte(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    size = 1024 * 1024 + 1
    (root / 'big.txt').write_bytes(b'x' * size)
    read = _tools(root)['Read']
    assert isinstance(read, Read)
    result = await read.execute('big.txt')
    assert isinstance(result.output, str)
    assert str(size) in result.output
    assert 'xxx' not in result.output


async def test_read_names_the_line_count_when_offset_is_past_the_end(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'a.txt').write_text('one\n', encoding='utf-8')
    read = _tools(root)['Read']
    assert isinstance(read, Read)
    result = await read.execute('a.txt', offset=5)
    assert isinstance(result.output, str)
    assert '行数 1' in result.output


async def test_write_outside_the_root_returns_output_and_does_not_raise(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    outside = tmp_path / 'escaped.txt'
    write = _tools(root)['Write']
    assert isinstance(write, Write)
    result = await write.execute('../escaped.txt', content='nope')
    assert isinstance(result.output, str)
    assert '期望:' in result.output
    assert not outside.exists()


async def test_edit_rejects_an_empty_old_string_and_leaves_the_file_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    target = root / 'a.txt'
    target.write_text('hello', encoding='utf-8')
    edit = _tools(root)['Edit']
    assert isinstance(edit, Edit)
    result = await edit.execute('a.txt', old_string='', new_string='x')
    assert '空串' in result.output
    assert target.read_text(encoding='utf-8') == 'hello'
