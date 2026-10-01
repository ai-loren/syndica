"""Glob / Grep 只返回工作区内的相对路径，并把搜索错误写进 output。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.kernel.errors import ConfigError
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


def _capped(root: Path, limit: int) -> dict[str, Tool]:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    return local_tools(workspace.portal(), output_limit=limit)


def _notes(root: Path, lines: list[str]) -> list[str]:
    """写一份 notes.txt，交回 Grep 搜 needle 时应得的每一条结果。"""
    root.mkdir(exist_ok=True)
    (root / 'notes.txt').write_text(''.join(f'{line}\n' for line in lines), encoding='utf-8')
    return [f'notes.txt:{number}:{line}' for number, line in enumerate(lines, start=1)]


async def test_grep_lists_the_hits_that_fit_and_says_how_many_there_were(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', [f'needle {"x" * size}' for size in (9, 2, 14, 5, 11, 3)])
    limit = len(hits[0]) + len(hits[1]) + len(hits[2]) + 3
    grep = _capped(tmp_path / 'ws', limit)['Grep']

    result = await grep.execute('needle')

    assert isinstance(result.output, list)
    assert result.output[:-1] == hits[:3]
    note = result.output[-1]
    assert '共 6 条' in note and '只列出前 3 条' in note and f'{limit} 个字符' in note
    assert '缩小 pattern 或 path' in note


async def test_glob_lists_the_paths_that_fit_and_says_how_many_there_were(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    names = [f'{"n" * size}.txt' for size in (7, 1, 12, 4, 9)]
    for name in names:
        (root / name).write_text('x', encoding='utf-8')
    paths = sorted(names)
    limit = len(paths[0]) + len(paths[1]) + 1
    glob = _capped(root, limit)['Glob']

    result = await glob.execute('*.txt')

    assert isinstance(result.output, list)
    assert result.output[:-1] == paths[:2]
    note = result.output[-1]
    assert '共 5 条' in note and '只列出前 2 条' in note and f'{limit} 个字符' in note
    assert '缩小 pattern' in note and 'path' not in note


async def test_hits_that_fill_the_limit_exactly_are_listed_whole(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', ['needle aa', 'needle b', 'needle cccc'])
    grep = _capped(tmp_path / 'ws', sum(len(hit) for hit in hits))['Grep']

    result = await grep.execute('needle')

    assert result.output == hits


async def test_one_character_past_the_limit_drops_the_last_hit(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', ['needle aa', 'needle b', 'needle cccc'])
    grep = _capped(tmp_path / 'ws', sum(len(hit) for hit in hits) - 1)['Grep']

    result = await grep.execute('needle')

    assert isinstance(result.output, list)
    assert result.output[:-1] == hits[:2]
    assert '共 3 条' in result.output[-1]


async def test_a_first_hit_longer_than_the_limit_is_clipped_and_said_to_be(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', ['needle ' + 'y' * 80, 'needle z'])
    grep = _capped(tmp_path / 'ws', 30)['Grep']

    result = await grep.execute('needle')

    assert isinstance(result.output, list)
    assert result.output[0] == hits[0][:30] + '…'
    assert len(result.output) == 2
    note = result.output[1]
    assert f'第一条结果有 {len(hits[0])} 个字符，只显示了前 30 个' in note
    assert 'Read' in note
    assert '共 2 条' in note and '缩小 pattern 或 path' in note


async def test_a_single_hit_too_long_to_show_says_so_without_other_hits(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', ['needle ' + 'y' * 80])
    grep = _capped(tmp_path / 'ws', 30)['Grep']

    result = await grep.execute('needle')

    assert isinstance(result.output, list)
    assert result.output[0] == hits[0][:30] + '…'
    assert f'第一条结果有 {len(hits[0])} 个字符' in result.output[1]
    assert '共 ' not in result.output[1]


async def test_hits_stay_in_line_order_past_the_ninth(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', [f'needle {index:02}' for index in range(12)])
    grep = _capped(tmp_path / 'ws', sum(len(hit) for hit in hits[:11]))['Grep']

    result = await grep.execute('needle')

    assert isinstance(result.output, list)
    assert result.output[:-1] == hits[:11]
    assert '共 12 条' in result.output[-1]


async def test_a_hit_that_does_not_fit_ends_the_list_even_if_later_ones_fit(
    tmp_path: Path,
) -> None:
    hits = _notes(tmp_path / 'ws', ['needle', 'needle ' + 'w' * 40, 'needle'])
    grep = _capped(tmp_path / 'ws', len(hits[0]) + len(hits[2]) + 1)['Grep']

    result = await grep.execute('needle')

    assert isinstance(result.output, list)
    assert result.output[0] == hits[0]
    assert len(result.output) == 2
    assert '共 3 条' in result.output[1] and '只列出前 1 条' in result.output[1]


async def test_the_budget_counts_characters_not_bytes(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', ['needle 中文搜索结果', 'needle 汉字也算一个'])
    grep = _capped(tmp_path / 'ws', sum(len(hit) for hit in hits))['Grep']

    result = await grep.execute('needle')

    assert result.output == hits


def test_search_descriptions_say_what_may_be_left_out(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    for name in ('Glob', 'Grep'):
        assert '可能只列出前面一部分' in tools[name].description
        assert '以 . 开头的目录与文件' in tools[name].description


def _cluttered(root: Path) -> Path:
    """一个带 .git、.venv、.env、字节码与 .github 的工作区；只有 src/app.py 是该搜到的源码。"""
    (root / 'src' / '__pycache__').mkdir(parents=True)
    (root / 'src' / 'app.py').write_text('needle = 1\n', encoding='utf-8')
    pyc = b'\xa7\r\r\n' + b'\x00' * 12 + b'needle'
    (root / 'src' / '__pycache__' / 'app.cpython-311.pyc').write_bytes(pyc)
    (root / '.git').mkdir()
    (root / '.git' / 'config').write_text('needle in git\n', encoding='utf-8')
    (root / '.venv' / 'lib').mkdir(parents=True)
    (root / '.venv' / 'lib' / 'dep.py').write_text('needle in venv\n', encoding='utf-8')
    (root / '.env').write_text('NEEDLE_KEY=needle\n', encoding='utf-8')
    (root / '.github' / 'workflows').mkdir(parents=True)
    (root / '.github' / 'workflows' / 'ci.yml').write_text('needle: ci\n', encoding='utf-8')
    return root


async def test_grep_skips_hidden_entries_and_binary_files(tmp_path: Path) -> None:
    grep = _tools(_cluttered(tmp_path / 'ws'))['Grep']

    result = await grep.execute('needle')

    assert result.output == ['src/app.py:1:needle = 1']


async def test_grep_searches_a_hidden_directory_or_file_it_is_pointed_at(tmp_path: Path) -> None:
    grep = _tools(_cluttered(tmp_path / 'ws'))['Grep']

    in_directory = await grep.execute('needle', path='.github')
    in_file = await grep.execute('needle', path='.env')

    assert in_directory.output == ['.github/workflows/ci.yml:1:needle: ci']
    assert in_file.output == ['.env:1:NEEDLE_KEY=needle']


async def test_glob_lists_hidden_entries_only_when_the_pattern_names_them(tmp_path: Path) -> None:
    glob = _tools(_cluttered(tmp_path / 'ws'))['Glob']

    assert (await glob.execute('**/*.py')).output == ['src/app.py']
    assert (await glob.execute('*')).output == ['src']
    assert (await glob.execute('.github/**/*.yml')).output == ['.github/workflows/ci.yml']
    assert (await glob.execute('.*')).output == ['.env', '.git', '.github', '.venv']


@pytest.mark.parametrize('tool', [Glob, Grep])
@pytest.mark.parametrize('limit', [0, -1, True, 2.5, '10'])
def test_search_tools_refuse_a_limit_that_is_not_a_positive_int(
    tmp_path: Path, tool: type[Glob] | type[Grep], limit: object
) -> None:
    portal = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={}).portal()

    with pytest.raises(ConfigError, match='output_limit 是正整数'):
        tool(portal, output_limit=limit)  # pyright: ignore[reportArgumentType]


async def test_without_a_limit_every_hit_is_listed(tmp_path: Path) -> None:
    hits = _notes(tmp_path / 'ws', [f'needle {index}' for index in range(500)])
    grep = _tools(tmp_path / 'ws')['Grep']

    result = await grep.execute('needle')

    assert result.output == hits


def test_search_tools_are_appended_and_do_not_scan_text_as_commands(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    tools = _tools(root)

    assert list(tools)[:5] == ['Read', 'Write', 'Edit', 'Glob', 'Grep']
    assert Glob.__tool_command_args__ == ()
    assert Grep.__tool_command_args__ == ()
