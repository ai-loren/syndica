"""Glob / Grep 只返回工作区内的相对路径，并把搜索错误写进 output。"""

from __future__ import annotations

from pathlib import Path

from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tool import Tool
from syndica.kernel.tools.local import Glob, Grep, local_tools


def _tools(root: Path) -> dict[str, Tool]:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    return local_tools(workspace.portal())


async def test_glob_does_not_return_paths_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('secret', encoding='utf-8')
    (root / 'inside.txt').write_text('inside', encoding='utf-8')
    (root / 'escaped.txt').symlink_to(outside)

    glob = _tools(root)['Glob']
    assert isinstance(glob, Glob)
    result = await glob.execute('*.txt')

    assert result.output == ['inside.txt']


async def test_glob_parent_pattern_is_reported_as_an_error(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    (tmp_path / 'outside.txt').write_text('secret', encoding='utf-8')

    glob = _tools(root)['Glob']
    assert isinstance(glob, Glob)
    result = await glob.execute('../outside.txt')

    assert isinstance(result.output, str)
    assert '期望:' in result.output
    assert '实际:' in result.output
    assert '下一步:' in result.output
    assert '相对模式' in result.output


async def test_grep_reports_an_invalid_regex_in_output(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    grep = _tools(root)['Grep']
    assert isinstance(grep, Grep)

    result = await grep.execute('[', path='.')

    assert isinstance(result.output, str)
    assert '期望:' in result.output
    assert '实际:' in result.output
    assert '下一步:' in result.output
    assert '合法的正则' in result.output
    assert '修正正则后再搜' in result.output


async def test_grep_results_are_relative_and_include_line_numbers(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    (root / 'nested').mkdir(parents=True)
    (root / 'nested' / 'notes.txt').write_text('first\nneedle here\nlast\n', encoding='utf-8')

    grep = _tools(root)['Grep']
    assert isinstance(grep, Grep)
    result = await grep.execute('needle')

    assert result.output == ['nested/notes.txt:2:needle here']


async def test_grep_limits_search_to_the_requested_path(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    (root / 'left').mkdir(parents=True)
    (root / 'right').mkdir()
    (root / 'left' / 'a.txt').write_text('needle left\n', encoding='utf-8')
    (root / 'right' / 'b.txt').write_text('needle right\n', encoding='utf-8')

    grep = _tools(root)['Grep']
    assert isinstance(grep, Grep)
    result = await grep.execute('needle', path='right')

    assert result.output == ['right/b.txt:1:needle right']


def test_search_tools_are_appended_and_do_not_scan_text_as_commands(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    tools = _tools(root)

    assert list(tools)[:5] == ['Read', 'Write', 'Edit', 'Glob', 'Grep']
    assert Glob.__tool_command_args__ == ()
    assert Grep.__tool_command_args__ == ()
