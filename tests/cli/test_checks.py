"""检查结果解析器（spec D10）：真实工具跑出的样例，加上样例覆盖不到的退出码与写法。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from syndica.cli.checks import CHECK_STATUSES, CHECK_TOOLS, CheckResult, parse_check

_FIXTURES = Path(__file__).parent / 'fixtures' / 'checks'
_PYTEST_KEYS = ('passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed')


def _pytest(**counts: int) -> dict[str, int]:
    return {key: counts.get(key, 0) for key in _PYTEST_KEYS}


# 人对每个样例「应该解析成什么」的判断，逐条写死，不从解析器的输出反推。
_EXPECTED: dict[str, CheckResult] = {
    'pytest_passed': CheckResult('pytest', 'passed', _pytest(passed=2), 'passed=2'),
    'pytest_failed': CheckResult(
        'pytest', 'failed', _pytest(passed=1, failed=1), 'passed=1 failed=1'
    ),
    'pytest_collection_error': CheckResult('pytest', 'error', _pytest(errors=1), 'errors=1'),
    'pytest_no_tests': CheckResult('pytest', 'no_tests', _pytest(), 'no tests ran'),
    'pytest_no_output': CheckResult('pytest', 'no_output', _pytest(), 'no summary in the output'),
    'pytest_timeout': CheckResult('pytest', 'timeout', _pytest(), 'timed out'),
    'ruff_check_passed': CheckResult(
        'ruff_check', 'passed', {'errors': 0, 'fixed': 0, 'fixable': 0}, 'passed'
    ),
    'ruff_check_failed': CheckResult(
        'ruff_check', 'failed', {'errors': 2, 'fixed': 0, 'fixable': 2}, 'errors=2 fixable=2'
    ),
    'ruff_format_passed': CheckResult(
        'ruff_format',
        'passed',
        {'would_reformat': 0, 'reformatted': 0, 'unchanged': 3},
        'unchanged=3',
    ),
    'ruff_format_failed': CheckResult(
        'ruff_format',
        'failed',
        {'would_reformat': 1, 'reformatted': 0, 'unchanged': 2},
        'would_reformat=1 unchanged=2',
    ),
    'pyright_passed': CheckResult(
        'pyright', 'passed', {'errors': 0, 'warnings': 0, 'informations': 0}, 'passed'
    ),
    'pyright_failed': CheckResult(
        'pyright', 'failed', {'errors': 1, 'warnings': 0, 'informations': 0}, 'errors=1'
    ),
    'check_deps_passed': CheckResult('check_deps', 'passed', {'violations': 0}, 'passed'),
    'check_deps_failed': CheckResult('check_deps', 'failed', {'violations': 1}, 'violations=1'),
    'check_deps_error': CheckResult('check_deps', 'error', {'violations': 0}, 'exit 2'),
    'check_links_passed': CheckResult('check_links', 'passed', {'broken': 0}, 'passed'),
    'check_links_failed': CheckResult('check_links', 'failed', {'broken': 1}, 'broken=1'),
    'other_passed': CheckResult('other', 'passed', {}, 'passed'),
    'other_failed': CheckResult('other', 'failed', {}, 'exit 1'),
}


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


def _parse(
    command: str,
    exit_code: int | None = 0,
    stdout: str = '',
    stderr: str = '',
    *,
    timed_out: bool = False,
) -> CheckResult:
    return parse_check(
        command=command, exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out
    )


def test_the_vocabulary_is_exactly_the_one_the_plan_names():
    assert CHECK_TOOLS == (
        'pytest',
        'ruff_check',
        'ruff_format',
        'pyright',
        'check_deps',
        'check_links',
        'other',
    )
    assert CHECK_STATUSES == ('passed', 'failed', 'error', 'timeout', 'no_output', 'no_tests')


def test_every_fixture_on_disk_has_an_expectation_and_the_reverse():
    assert sorted(path.stem for path in _FIXTURES.glob('*.json')) == sorted(_EXPECTED)


def test_the_fixtures_cover_the_classes_the_roadmap_requires():
    """每个工具至少通过与失败两类；pytest 另有收集错误、超时、无输出与没有测试。"""
    covered = {(result.tool, result.status) for result in _EXPECTED.values()}

    assert {
        ('pytest', 'passed'),
        ('pytest', 'failed'),
        ('pytest', 'error'),
        ('pytest', 'timeout'),
        ('pytest', 'no_output'),
        ('pytest', 'no_tests'),
        ('ruff_check', 'passed'),
        ('ruff_check', 'failed'),
        ('ruff_format', 'passed'),
        ('ruff_format', 'failed'),
        ('pyright', 'passed'),
        ('pyright', 'failed'),
        ('check_deps', 'passed'),
        ('check_deps', 'failed'),
        ('check_links', 'passed'),
        ('check_links', 'failed'),
        ('other', 'passed'),
        ('other', 'failed'),
    } <= covered


@pytest.mark.parametrize('name', sorted(_EXPECTED))
def test_each_real_fixture_parses_to_its_expected_result(name: str):
    fixture = _fixture(name)

    result = parse_check(**fixture)  # type: ignore[arg-type]

    expected = _EXPECTED[name]
    assert result == expected
    assert list(result.counts) == list(expected.counts)


@pytest.mark.parametrize(
    ('command', 'tool'),
    [
        ('pytest', 'pytest'),
        ('py.test -q', 'pytest'),
        ('python -m pytest -x tests/cli', 'pytest'),
        ('.venv/bin/pytest -q', 'pytest'),
        ('"./.venv/bin/pytest" -q', 'pytest'),
        ('cd sub && uv run pytest -q', 'pytest'),
        ('(cd sub; pytest)', 'pytest'),
        ('uv run --with pytest-cov pytest', 'pytest'),
        ('FOO=1 uv run pytest', 'pytest'),
        ('env -u X PYTHONHASHSEED=0 pytest', 'pytest'),
        ('timeout 60 uv run pytest', 'pytest'),
        ('uv run pytest -q 2>&1 | tail -20', 'pytest'),
        ('uv run pytest -q; echo "exit=$?"', 'pytest'),
        ('uv run ruff check --fix .', 'ruff_check'),
        ('uv run ruff -q check .', 'ruff_check'),
        ('ruff --config x.toml check .', 'ruff_check'),
        ('uv run ruff format .', 'ruff_format'),
        ('uv run ruff --quiet format --check .', 'ruff_format'),
        ('uv run pyright src', 'pyright'),
        ('uvx pyright@1.1.411', 'pyright'),
        ('uv run python scripts/check_deps.py', 'check_deps'),
        ('python ./scripts/check_links.py', 'check_links'),
    ],
)
def test_the_tool_is_recognised_in_the_command_position(command: str, tool: str):
    assert _parse(command).tool == tool


@pytest.mark.parametrize(
    'command',
    [
        'uv run ruff check . && uv run pytest -q',
        'uv run pytest -q tests/a; uv run pytest -q tests/b',
        'uv run ruff check . || uv run ruff check --fix .',
        'for f in tests/*.py; do uv run pytest -q "$f"; done',
        'git commit -m "make pytest pass"',
        'git commit -m "fix: ruff check errors"',
        'grep -rn pytest src/syndica/cli/',
        'echo pytest',
        'which pytest',
        'uv add --dev pytest',
        'uv add pytest-cov',
        'pip install pytest ruff',
        'pytest --version',
        'pyright --version',
        'ruff check --help',
        'uv run ruff --version',
        "sh -c 'pytest -q'",
        'echo "unbalanced',
        'ls',
    ],
)
def test_other_commands_and_several_checks_in_one_command_are_other(command: str):
    """同一个工具跑两次也是 `other`：前一次的失败会被后一次的汇总盖住。只有命令词位置上的名字算，
    提交说明、grep 的模式与 `--version` 都不是一次检查。"""
    assert _parse(command).tool == 'other'


def test_a_very_long_command_is_other_and_recognition_stays_fast():
    """一次检查的命令不会有几千个字符；分词是纯 Python、同步跑在事件循环上。"""
    started = time.process_time()

    assert _parse('uv run pytest -q ' + 'tests/a.py ' * 1000).tool == 'other'
    assert _parse('ruff ' * 800_000 + 'check .').tool == 'other'
    assert time.process_time() - started < 2


@pytest.mark.parametrize(
    ('command', 'output', 'status'),
    [
        ('uv run pytest -q | tail -3', '1 failed, 3 passed in 0.20s\n', 'failed'),
        ('uv run pytest -q | tail -3', '1 passed, 2 errors in 0.20s\n', 'error'),
        ('uv run pytest -q | tail -3', '1 failed, 1 error in 0.20s\n', 'error'),
        ('uv run pytest -q -k nope; true', '4 deselected in 0.01s\n', 'no_tests'),
        ('uv run pytest -q | tail -1', 'no tests ran in 0.01s\n', 'no_tests'),
        ('uv run pytest -q | tail -1', '3 skipped in 0.01s\n', 'passed'),
        ('uv run ruff check . || true', 'Found 3 errors.\n', 'failed'),
        ('uv run ruff format --check . | cat', '2 files would be reformatted\n', 'failed'),
        ('uv run pyright | cat', '2 errors, 0 warnings, 0 informations \n', 'failed'),
        ('uv run python scripts/check_deps.py; true', '共 2 处违规。\n', 'failed'),
        ('uv run python scripts/check_links.py; true', '共 1 处断链。\n', 'failed'),
    ],
)
def test_a_zero_exit_code_does_not_hide_the_tools_own_failure(
    command: str, output: str, status: str
):
    """管道与 `; true` 交回的是最后一条命令的退出码。全部跳过的一次 run 仍是通过。"""
    assert _parse(command, 0, output).status == status


@pytest.mark.parametrize(
    ('exit_code', 'output', 'status', 'counts'),
    [
        (0, 'Found 2 errors (2 fixed, 0 remaining).\n', 'passed', (0, 2, 0)),
        (1, 'Found 3 errors (2 fixed, 1 remaining).\n', 'failed', (1, 2, 0)),
        (0, 'Found 3 errors (2 fixed, 1 remaining).\n', 'failed', (1, 2, 0)),
    ],
)
def test_ruff_fix_reports_what_remains(
    exit_code: int, output: str, status: str, counts: tuple[int, int, int]
):
    result = _parse('uv run ruff check --fix .', exit_code, output)

    assert (result.status, tuple(result.counts.values())) == (status, counts)


def test_coloured_ruff_output_is_read_through_its_escape_codes():
    output = 'Found 2 errors.\n[\x1b[36m*\x1b[0m] 2 fixable with the `--fix` option.\n'

    assert dict(_parse('uv run ruff check .', 1, output).counts) == {
        'errors': 2,
        'fixed': 0,
        'fixable': 2,
    }


def test_only_the_last_summary_line_counts_even_inside_a_diff():
    """`--diff` 把文件内容带进输出，文件里可能正好写着一行汇总的原文；按键分别取最后一次出现会把
    两行拼成一个不存在的汇总。"""
    output = (
        "+    ('ruff format --check .', '2 files reformatted, 5 files left unchanged\\n'),\n"
        '1 file would be reformatted, 3 files already formatted\n'
    )

    result = _parse('uv run ruff format --check --diff .', 1, output)

    assert dict(result.counts) == {'would_reformat': 1, 'reformatted': 0, 'unchanged': 3}


def test_a_formatter_that_rewrote_files_passed():
    result = _parse('uv run ruff format .', 0, '2 files reformatted, 5 files left unchanged\n')

    assert result.status == 'passed'
    assert dict(result.counts) == {'would_reformat': 0, 'reformatted': 2, 'unchanged': 5}


@pytest.mark.parametrize(
    ('output', 'counts'),
    [
        ('0 errors, 1 warning, 1 information \n', (0, 1, 1)),
        ('2 errors, 3 warnings, 4 informations \n', (2, 3, 4)),
    ],
)
def test_pyright_counts_are_read_in_both_numbers(output: str, counts: tuple[int, int, int]):
    assert tuple(_parse('uv run pyright', 0, output).counts.values()) == counts


def test_pyright_update_noise_after_the_summary_does_not_hide_it():
    output = (
        '0 errors, 0 warnings, 0 informations \n'
        'WARNING: there is a new pyright version available (v1.1.411 -> v1.1.414).\n'
        'Please install the new version or set PYRIGHT_PYTHON_FORCE_VERSION to `latest`\n'
    )

    assert _parse('uv run pyright', 0, output).status == 'passed'


@pytest.mark.parametrize(
    ('command', 'status'),
    [
        ('uv run pytest -q', 'no_output'),
        ('uv run ruff check .', 'no_output'),
        ('uv run ruff format --check .', 'no_output'),
        ('uv run pyright', 'no_output'),
        ('uv run python scripts/check_deps.py', 'no_output'),
        ('uv run python scripts/check_links.py', 'no_output'),
        ('make build', 'passed'),
    ],
)
def test_success_without_a_summary_is_not_trusted(command: str, status: str):
    """退出码 0 可能来自 `| tail` 或 `; echo`：工具自己的汇总不在，就确认不了它通过了。"""
    assert _parse(command, 0, 'Traceback (most recent call last):\n  ...\n').status == status


@pytest.mark.parametrize(
    'command', ['uv run python scripts/check_deps.py', 'uv run python scripts/check_links.py']
)
def test_a_quiet_script_passes_only_when_it_is_silent(command: str):
    assert _parse(command, 0).status == 'passed'
    assert _parse(command, 0, '', ' \n').status == 'passed'
    assert _parse(command, 1, 'Traceback (most recent call last):\n').status == 'error'


@pytest.mark.parametrize(
    ('command', 'exit_code', 'output', 'status'),
    [
        ('uv run pytest -q', 1, 'No module named pytest\n', 'error'),
        ('uv run pytest -q', 1, '1 passed, 1 error in 0.10s\n', 'error'),
        ('uv run pytest -q', 1, '3 passed in 0.10s\n', 'failed'),
        ('uv run pytest -q', 2, 'whatever\n', 'error'),
        ('uv run pytest -q', 3, 'whatever\n', 'error'),
        ('uv run pytest -q', 4, 'whatever\n', 'error'),
        ('uv run pytest -q', 5, 'whatever\n', 'no_tests'),
        ('uv run pytest -q', 127, 'whatever\n', 'error'),
        ('uv run ruff check --output-format json .', 1, '[{"code": "F401"}]\n', 'failed'),
        ('uv run ruff check .', 2, 'error: bad config\n', 'error'),
        ('uv run pyright', 1, 'whatever\n', 'failed'),
        ('uv run pyright', 3, 'whatever\n', 'error'),
        ('uv run python scripts/check_links.py', 1, '共 1 处断链。\n', 'failed'),
        ('uv run python scripts/check_links.py', 1, 'Traceback\n', 'error'),
        ('uv run python scripts/check_links.py', 2, 'whatever\n', 'error'),
        ('ls missing', 2, 'whatever\n', 'failed'),
    ],
)
def test_a_non_zero_exit_code_follows_each_tools_own_table(
    command: str, exit_code: int, output: str, status: str
):
    """pytest 的退出码 1 没有汇总时多半是它根本没跑起来（缺模块、超时插件 `os._exit(1)`）。"""
    assert _parse(command, exit_code, output).status == status


def test_a_timeout_wrapper_exit_code_is_a_timeout():
    assert _parse('timeout 60 uv run pytest -q', 124, 'tests/a.py ..\n').status == 'timeout'
    assert _parse('uv run pytest -q', 124, 'tests/a.py ..\n').status == 'error'


def test_a_result_without_an_exit_code_that_did_not_time_out_is_an_error():
    assert _parse('uv run pytest -q', None).summary == 'no exit code'
    assert _parse('uv run pytest -q', None, '2 passed in 0.01s\n').status == 'error'


def test_a_timeout_wins_over_any_partial_summary():
    result = _parse('uv run pytest -q', None, '2 passed in 0.01s\n', timed_out=True)

    assert result.status == 'timeout'


@pytest.mark.parametrize(
    ('output', 'counts'),
    [
        ('1 passed, 2 xfailed, 3 xpassed in 0.10s\n', {'passed': 1, 'xfailed': 2, 'xpassed': 3}),
        ('1 passed, 4 skipped, 2 deselected, 3 warnings in 0.10s\n', {'passed': 1, 'skipped': 4}),
        ('2 failed, 1 passed, 2 subtests passed in 0.03s\n', {'failed': 2, 'passed': 1}),
        ('1 passed, 1 warning in 83.21s (0:01:23)\n', {'passed': 1}),
        ('========== 1 failed, 1 passed in 0.04s ==========\n', {'failed': 1, 'passed': 1}),
    ],
)
def test_every_item_of_a_pytest_summary_is_read(output: str, counts: dict[str, int]):
    """子测试的计数不并进测试的计数；warnings 与 deselected 是汇总里合法的项，但不计数。"""
    result = _parse('uv run pytest -q', 1 if 'failed' in output else 0, output)

    assert dict(result.counts) == {key: counts.get(key, 0) for key in _PYTEST_KEYS}


def test_the_last_pytest_summary_line_counts():
    output = '3 passed in 0.10s\nrerunning\n1 failed, 2 passed in 0.30s\n'

    assert _parse('uv run pytest -q', 1, output).summary == 'passed=2 failed=1'


@pytest.mark.parametrize(
    'line',
    [
        'tests/x.py::test_a[uv run pytest -q -k nope; true-4 deselected in 0.01s] PASSED [ 50%]',
        'note: 1 failed login in the cache',
        'tests/x.py::test_b[1 failed, 3 passed in 0.20s-failed]',
        '3 passed',
    ],
)
def test_a_line_that_merely_contains_a_summary_is_not_the_summary(line: str):
    """`-v` 的测试 id 与别的输出里可能正好含着一段像汇总的话；整行形状对才算。"""
    assert _parse('uv run pytest -v | head -60', 0, f'{line}\n').status == 'no_output'
    timed_out = _parse('uv run pytest -v', None, f'{line}\n', timed_out=True)
    assert (timed_out.status, timed_out.summary) == ('timeout', 'timed out')


def test_a_count_outside_the_summary_line_is_ignored():
    result = _parse(
        'uv run pytest -q', 0, '2 passed in 0.01s\n', 'note: 1 failed login in the cache\n'
    )

    assert (result.status, result.summary) == ('passed', 'passed=2')


def test_the_two_streams_are_read_as_separate_lines():
    assert _parse('uv run pytest -q', 0, 'progress', '2 passed in 0.01s\n').status == 'passed'


def test_the_summary_is_found_on_stderr_too():
    assert _parse('uv run pytest -q', 0, '', '2 passed in 0.01s\n').status == 'passed'


def test_coloured_output_is_read_through_its_escape_codes():
    output = '\x1b[32m\x1b[1m2 passed\x1b[0m\x1b[32m in 0.01s\x1b[0m\n'

    assert _parse('uv run pytest -q --color=yes', 0, output).summary == 'passed=2'


@pytest.mark.parametrize(
    ('command', 'template'),
    [
        ('uv run pytest -q', '{} passed in 0.01s\n'),
        ('uv run ruff check .', 'Found {} errors.\n'),
        ('uv run ruff format --check .', '{} files would be reformatted\n'),
        ('uv run pyright', '{} errors, 0 warnings, 0 informations\n'),
        ('uv run python scripts/check_deps.py', '共 {} 处违规。\n'),
    ],
)
def test_a_count_too_long_for_an_int_is_not_a_summary(command: str, template: str):
    """Python 3.11 的 int() 拒绝四千多位的数字，而 Agent 能在输出里打印任意一行。"""
    result = _parse(command, 0, template.format('9' * 4301))

    assert result.status == 'no_output'


def test_pathological_output_is_parsed_in_linear_time():
    """宿主复核交来的输出不截断；回溯型正则在这些行上是指数或平方时间。"""
    started = time.process_time()

    _parse('uv run pytest -q', 1, '1 passed, ' * 100_000 + '1 passed in 0.10s\n')
    _parse('uv run pytest -q', 1, '1' * 40 + 'x passed in 0.10s\n')
    _parse('uv run pytest -q', 1, ('x ' * 50_000 + '\n') * 20)

    assert time.process_time() - started < 2


def test_the_result_cannot_be_changed_after_the_fact():
    result = _parse('uv run pytest -q', 0, '2 passed in 0.01s\n')

    with pytest.raises(TypeError):
        result.counts['passed'] = 9  # type: ignore[index]
    with pytest.raises(AttributeError):
        result.status = 'failed'  # type: ignore[misc]
