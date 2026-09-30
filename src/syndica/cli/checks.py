"""检查结果解析器（spec D10）：一次命令的结构化结果 → 这次检查的摘要。

纯函数，输入是 `(command, exit_code, stdout, stderr, timed_out)`：Bash 的 `meta` 或宿主复核的
`ExecResult`。认工具只看命令文本，只用于摘要，不参与任何安全判定——Agent 写得出任何像 pytest 的命令。

摘要宁可说「确认不了」也不说「通过」：退出码能被管道、`; echo`、`|| true` 换掉，所以通过要有这个
工具自己的汇总作证。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

__all__ = ['CHECK_STATUSES', 'CHECK_TOOLS', 'CheckResult', 'parse_check']

CHECK_TOOLS: tuple[str, ...] = (
    'pytest',
    'ruff_check',
    'ruff_format',
    'pyright',
    'check_deps',
    'check_links',
    'other',
)
CHECK_STATUSES: tuple[str, ...] = (
    'passed',
    'failed',
    'error',
    'timeout',
    'no_output',
    'no_tests',
)


@dataclass(frozen=True)
class CheckResult:
    """`counts` 的键与键序每个工具固定（`other` 没有计数），输出里没有的计数是 0。`summary` 是计数里
    不为 0 的几项（`passed=1 failed=1`），一项都没有时是状态的一句话；它由计数拼出、不抄工具的原文，
    Agent 的输出进不了摘要。

    `no_output`：退出码是 0，但输出里没有这个工具自己的汇总（被重定向、被 `head` 截掉），确认不了它
    真的跑过。成功时本来就不出声的 check_deps 与 check_links，两条流都是空的才算通过。
    """

    tool: str
    status: str
    counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    summary: str = ''


_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        'pytest': ('passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed'),
        # `--fix` 之后 errors 是还剩下的：`Found 3 errors (2 fixed, 1 remaining).`
        'ruff_check': ('errors', 'fixed', 'fixable'),
        'ruff_format': ('would_reformat', 'reformatted', 'unchanged'),
        'pyright': ('errors', 'warnings', 'informations'),
        'check_deps': ('violations',),
        'check_links': ('broken',),
        'other': (),
    }
)
# 退出码是 0 时，汇总里这一项不为 0 就是失败（管道与 `; true` 交回的是最后一条命令的退出码）。
_FAILING_COUNT: Mapping[str, str] = MappingProxyType(
    {
        'ruff_check': 'errors',
        'ruff_format': 'would_reformat',
        'pyright': 'errors',
        'check_deps': 'violations',
        'check_links': 'broken',
    }
)
_QUIET = frozenset({'check_deps', 'check_links'})

# 计数最多 9 位：再长 `int()` 在 3.11 上抛 ValueError（整数与字符串互转的位数上限），而 Agent 能
# 在输出里打印任意一行。
_COUNT = r'(\d{1,9})'
_DURATION = r' in \d{1,9}(?:\.\d{1,9})?s(?: \(\d{1,9}:\d\d:\d\d\))?'
# pytest 的汇总行按整行的形状认：`-v` 下测试 id 里可能正好含着一段 `1 failed in 0.2s`。逐项
# fullmatch、不用嵌套量词：宿主复核交来的输出不截断。
_PYTEST_LINE = re.compile(rf'(.+){_DURATION}')
_PYTEST_ITEM = re.compile(
    rf'{_COUNT} (subtests? )?'
    r'(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)'
)
_PYTEST_NO_TESTS = re.compile(rf'no tests ran{_DURATION}')
_PYTEST_KEYS: Mapping[str, str] = MappingProxyType(
    {
        'passed': 'passed',
        'failed': 'failed',
        'error': 'errors',
        'errors': 'errors',
        'skipped': 'skipped',
        'xfailed': 'xfailed',
        'xpassed': 'xpassed',
    }
)
_RUFF_FOUND = re.compile(rf'Found {_COUNT} errors?(?: \({_COUNT} fixed, {_COUNT} remaining\))?\.')
_RUFF_FIXABLE = re.compile(rf'\[\*\] {_COUNT} fixable\b')
_FORMAT_ITEM = re.compile(
    rf'{_COUNT} files? (would be reformatted|reformatted|already formatted|left unchanged)'
)
_FORMAT_KEYS: Mapping[str, str] = MappingProxyType(
    {
        'would be reformatted': 'would_reformat',
        'reformatted': 'reformatted',
        'already formatted': 'unchanged',
        'left unchanged': 'unchanged',
    }
)
_PYRIGHT = re.compile(rf'{_COUNT} errors?, {_COUNT} warnings?, {_COUNT} informations?')
_SCRIPT_SUMMARY: Mapping[str, re.Pattern[str]] = MappingProxyType(
    {
        'check_deps': re.compile(rf'共 {_COUNT} 处违规。'),
        'check_links': re.compile(rf'共 {_COUNT} 处断链。'),
    }
)
_ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')

# 认工具只看这么长的命令：一次检查的命令不会更长，而 shlex 分词是纯 Python，判定同步跑在事件循环上。
_MAX_COMMAND = 8192
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
_PYTHON = re.compile(r'python(?:\d+(?:\.\d+)?)?')
# 这些启动器之后才是真正的命令词；它们要吃掉下一个词的选项另列。
_LAUNCHER_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        'env': frozenset({'-u', '--unset', '-C', '--chdir', '-S', '--split-string'}),
        'nice': frozenset({'-n', '--adjustment'}),
        'timeout': frozenset({'-s', '--signal', '-k', '--kill-after'}),
        'uvx': frozenset({'--from', '--with', '-p', '--python', '--index', '--index-url'}),
        'npx': frozenset({'-p', '--package'}),
        'command': frozenset(),
        'exec': frozenset(),
        'nohup': frozenset(),
        'time': frozenset(),
    }
)
_UV_RUN_VALUES = frozenset(
    {
        '--with',
        '--with-editable',
        '--with-requirements',
        '-w',
        '--python',
        '-p',
        '--project',
        '--directory',
        '--package',
        '--extra',
        '--group',
        '--env-file',
        '--index',
        '--index-url',
        '--extra-index-url',
        '--find-links',
        '-f',
    }
)
_NOT_A_RUN = frozenset({'--version', '-V', '--help', '-h'})


def parse_check(
    *, command: str, exit_code: int | None, stdout: str, stderr: str, timed_out: bool
) -> CheckResult:
    """一次命令的检查摘要。退出码是非 0 时状态按这个工具自己的退出码表；是 0 时看它自己的汇总，汇总
    里有失败就不报 passed，没有汇总就是 `no_output`。"""
    tool, wrapped_in_timeout = _invocation(command)
    lines = _ANSI.sub('', f'{stdout}\n{stderr}').splitlines()
    counts, summarized = _summary(tool, lines)
    # `timeout N cmd` 超时时交回 124：命令没跑完，与宿主自己的超时是同一件事。
    timed_out = timed_out or (wrapped_in_timeout and exit_code == 124)
    silent = not (stdout.strip() or stderr.strip())
    status = _status(tool, exit_code, timed_out, counts, summarized, silent)
    shown = ' '.join(f'{key}={number}' for key, number in counts.items() if number)
    return CheckResult(
        tool=tool,
        status=status,
        counts=MappingProxyType(counts),
        summary=shown or _status_phrase(status, exit_code),
    )


def _invocation(command: str) -> tuple[str, bool]:
    """(命令里跑的那一个工具, 它是否套在 `timeout` 里)。按 shell 的段（`;` `&&` `||` `|` `&`、括号
    与换行）逐段看命令词；一个工具都没有、或有两段各跑了工具（`pytest a; pytest b` 的输出混在
    一起，前一次失败会被后一次的汇总盖住）时是 `other`。"""
    if len(command) > _MAX_COMMAND:
        return 'other', False
    try:
        lexer = shlex.shlex(command.replace('\n', ' ; '), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return 'other', False
    found: list[tuple[str, bool]] = []
    segment: list[str] = []
    for token in [*tokens, ';']:
        if token and all(char in ';&|()' for char in token):
            recognised = _segment_tool(segment)
            if recognised is not None:
                found.append(recognised)
            segment = []
        else:
            segment.append(token)
    return found[0] if len(found) == 1 else ('other', False)


def _segment_tool(words: Sequence[str]) -> tuple[str, bool] | None:
    """一段命令在命令词位置上跑的检查工具：跳过变量赋值与启动器（`uv run`、`python -m`、`timeout`、
    `env`……），`--version` / `--help` 不算一次检查。"""
    index = 0
    wrapped_in_timeout = False
    while index < len(words):
        word = words[index].rsplit('/', 1)[-1]
        if _ASSIGNMENT.match(word):
            index += 1
        elif word in _LAUNCHER_VALUES:
            wrapped_in_timeout = wrapped_in_timeout or word == 'timeout'
            index = _after_options(words, index + 1, _LAUNCHER_VALUES[word])
            if word == 'timeout':
                index += 1
        elif word == 'uv' and index + 1 < len(words) and words[index + 1] == 'run':
            index = _after_options(words, index + 2, _UV_RUN_VALUES)
        elif _PYTHON.fullmatch(word):
            index += 1
            while index < len(words) and words[index].startswith('-') and words[index] != '-m':
                index += 1
            if index < len(words) and words[index] == '-m':
                index += 1
            break
        else:
            break
    if index >= len(words) or _NOT_A_RUN.intersection(words[index + 1 :]):
        return None
    program = words[index].rsplit('/', 1)[-1].split('@', 1)[0]
    if program in ('pytest', 'py.test'):
        return 'pytest', wrapped_in_timeout
    if program == 'pyright':
        return 'pyright', wrapped_in_timeout
    if program in ('check_deps.py', 'check_links.py'):
        return program.removesuffix('.py'), wrapped_in_timeout
    if program == 'ruff':
        position = _after_options(words, index + 1, frozenset({'--config'}))
        subcommand = words[position] if position < len(words) else ''
        if subcommand in ('check', 'format'):
            return f'ruff_{subcommand}', wrapped_in_timeout
    return None


def _after_options(words: Sequence[str], index: int, valued: frozenset[str]) -> int:
    """从 index 起跳过选项（`valued` 里的连同它的值），交回第一个不是选项的位置，可能越界。"""
    while index < len(words) and words[index].startswith('-'):
        index += 2 if words[index] in valued else 1
    return index


def _summary(tool: str, lines: Sequence[str]) -> tuple[dict[str, int], bool]:
    """(计数, 输出里有没有这个工具自己的汇总)。每个工具只认最后一行形状对的汇总：`--diff` 与测试 id
    会把汇总的原文带进输出。"""
    counts = dict.fromkeys(_KEYS[tool], 0)
    if tool == 'pytest':
        return counts, _pytest_summary(lines, counts)
    if tool == 'ruff_check':
        return counts, _ruff_check_summary(lines, counts)
    for line in reversed(lines):
        stripped = line.strip()
        if tool == 'ruff_format':
            items = [_FORMAT_ITEM.fullmatch(item) for item in stripped.split(', ')]
            if stripped and all(items):
                for item in items:
                    if item is not None:
                        counts[_FORMAT_KEYS[item.group(2)]] = int(item.group(1))
                return counts, True
        elif tool == 'pyright':
            found = _PYRIGHT.fullmatch(stripped)
            if found:
                counts.update(zip(_KEYS['pyright'], map(int, found.groups()), strict=True))
                return counts, True
        elif tool in _SCRIPT_SUMMARY:
            found = _SCRIPT_SUMMARY[tool].fullmatch(stripped)
            if found:
                counts[_KEYS[tool][0]] = int(found.group(1))
                return counts, True
    return counts, False


def _pytest_summary(lines: Sequence[str], counts: dict[str, int]) -> bool:
    for line in reversed(lines):
        core = line.strip().strip('=').strip()
        if _PYTEST_NO_TESTS.fullmatch(core):
            return True
        whole = _PYTEST_LINE.fullmatch(core)
        if whole is None:
            continue
        items = [_PYTEST_ITEM.fullmatch(item) for item in whole.group(1).split(', ')]
        if not all(items):
            continue
        for item in items:
            if item is not None and not item.group(2):
                key = _PYTEST_KEYS.get(item.group(3))
                if key is not None:
                    counts[key] = int(item.group(1))
        return True
    return False


def _ruff_check_summary(lines: Sequence[str], counts: dict[str, int]) -> bool:
    """`Found …` 那一行与它后面的 `[*] N fixable`；干净时是 `All checks passed!`。"""
    fixable = 0
    for line in reversed(lines):
        stripped = line.strip()
        if stripped == 'All checks passed!':
            return True
        hint = _RUFF_FIXABLE.match(stripped)
        if hint and not fixable:
            fixable = int(hint.group(1))
            continue
        found = _RUFF_FOUND.fullmatch(stripped)
        if found:
            total, fixed, remaining = found.groups()
            counts['errors'] = int(total if remaining is None else remaining)
            counts['fixed'] = 0 if fixed is None else int(fixed)
            counts['fixable'] = fixable
            return True
    return False


def _status(
    tool: str,
    exit_code: int | None,
    timed_out: bool,
    counts: Mapping[str, int],
    summarized: bool,
    silent: bool,
) -> str:
    if timed_out:
        return 'timeout'
    if exit_code is None:
        return 'error'
    if tool == 'other':
        return 'passed' if exit_code == 0 else 'failed'
    if tool == 'pytest':
        return _pytest_status(exit_code, counts, summarized)
    failing = counts[_FAILING_COUNT[tool]]
    if exit_code == 0:
        if summarized:
            return 'failed' if failing else 'passed'
        if tool in _QUIET:
            return 'passed' if silent else 'no_output'
        return 'no_output'
    if exit_code == 1:
        # 本来不出声的脚本崩溃时也退出 1，但没有它自己的「共 N 处」那一行。
        return 'error' if tool in _QUIET and not summarized else 'failed'
    return 'error'


def _pytest_status(exit_code: int, counts: Mapping[str, int], summarized: bool) -> str:
    """退出码 1 也可能是 fixture 出错、收集错误（`--continue-on-collection-errors`）或 pytest 根本
    没跑起来：有汇总时 errors 排在 failed 前面，没有汇总是 error。"""
    if exit_code == 5:
        return 'no_tests'
    if exit_code not in (0, 1):
        return 'error'
    if not summarized:
        return 'no_output' if exit_code == 0 else 'error'
    if counts['errors']:
        return 'error'
    if counts['failed'] or exit_code == 1:
        return 'failed'
    return 'passed' if any(counts.values()) else 'no_tests'


def _status_phrase(status: str, exit_code: int | None) -> str:
    if status == 'timeout':
        return 'timed out'
    if status == 'no_output':
        return 'no summary in the output'
    if status == 'no_tests':
        return 'no tests ran'
    if status == 'passed':
        return 'passed'
    return 'no exit code' if exit_code is None else f'exit {exit_code}'
