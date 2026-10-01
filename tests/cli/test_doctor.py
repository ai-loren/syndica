"""`doctor` 与 `doctor --verify`（spec D16）：自检在 Agent 的环境里做，输出行顺序固定。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import syndica.cli.host as host_module
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, CliOutcome, main
from syndica.kernel import ScriptedLLM

_SECRET = 'Zq9' + 'Lm4Xw7Pk2Rt8Vb5Nc3Jd6'
_FAKE_PYTEST = (
    '#!/bin/sh\n'
    'if [ "$1" = "--version" ]; then echo "pytest 8.0.0"; exit 0; fi\n'
    'echo "F.."\n'
    'echo "=================== 1 failed, 2 passed in 0.12s ==================="\n'
    'exit 1\n'
)


def _config(tmp_path: Path, **workspace: object) -> Path:
    path = tmp_path / 'syndica.yaml'
    lines = ['version: 1']
    if workspace:
        lines.append(f'workspace: {json.dumps(workspace)}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def _tool(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(body, encoding='utf-8')
    path.chmod(0o755)


def _path(tools: Path) -> str:
    return f'{tools}:{os.environ["PATH"]}'


def _environ(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    environ = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return {
        **environ,
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
        **extra,
    }


def _workspace(tmp_path: Path, *, git: bool = True, name: str = 'ws') -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / 'README.md').write_text('# project\n', encoding='utf-8')
    if git:
        environ = _environ(tmp_path)
        for args in (
            ['init', '-q'],
            ['add', '.'],
            ['-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '-m', 'init'],
        ):
            subprocess.run(['git', *args], cwd=root, env=environ, check=True, capture_output=True)
    return root


def _doctor(
    tmp_path: Path, root: Path, config: Path, *, verify: bool = False, **extra: str
) -> CliOutcome:
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=root,
        environ=_environ(tmp_path, **extra),
    )
    try:
        return host.doctor(str(config), verify=verify)
    finally:
        host.close()


def _lines(outcome: CliOutcome, prefix: str) -> list[str]:
    return [line for line in outcome.output.splitlines() if line.startswith(prefix)]


def _line(outcome: CliOutcome, prefix: str) -> str:
    (line,) = _lines(outcome, prefix)
    return line


def _no_raw_controls(outcome: CliOutcome) -> None:
    assert [c for c in outcome.output if ord(c) < 0x20 and c != '\n'] == []


# ---- 行与顺序 ----


def test_the_lines_come_in_a_fixed_order(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['true']))

    prefixes = [line.split(':', 1)[0] for line in outcome.output.splitlines()]
    assert prefixes == ['config', 'models', 'workspace', 'env', 'git', 'python', 'exe']
    assert outcome.exit_code == 0
    assert _line(outcome, 'exe:') == 'exe: ok true（shell 内建命令）'
    assert _line(outcome, 'python:').startswith('python: ok（Python 3.')


def test_paths_in_the_config_and_workspace_lines_are_escaped(tmp_path: Path):
    root = _workspace(tmp_path, git=False, name='ws\x1b[31mRED')

    outcome = _doctor(tmp_path, root, _config(root))

    _no_raw_controls(outcome)
    assert 'ws\\x1b[31mRED' in _line(outcome, 'workspace:')
    assert 'ws\\x1b[31mRED' in _line(outcome, 'config:')


def test_the_env_line_has_names_only(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(
        tmp_path, root, _config(tmp_path, pass_env=['PROBE_NAME']), PROBE_NAME='value-not-shown'
    )

    assert 'PROBE_NAME' in _line(outcome, 'env:').split()
    assert 'value-not-shown' not in outcome.output


# ---- 可执行文件 ----


def test_a_verify_executable_missing_from_the_agent_environment_fails_with_a_path_hint(
    tmp_path: Path,
):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['nosuchtool-xyz run']))

    assert outcome.exit_code == 1
    line = _line(outcome, 'exe:')
    assert line.startswith('exe: failed nosuchtool-xyz（Agent 的环境里找不到它）。下一步：')
    assert '把它所在的目录加进 PATH' in line
    assert 'workspace.pass_env' in line


def test_the_version_is_probed_in_the_agent_environment_not_the_hosts(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'probe-tool', '#!/bin/sh\n[ -n "$ONLY_HOST" ] || exit 3\necho "probe 1.0"\n')

    hidden = _doctor(
        tmp_path,
        root,
        _config(tmp_path, verify=['probe-tool check']),
        PATH=_path(tools),
        ONLY_HOST='1',
    )
    passed = _doctor(
        tmp_path,
        root,
        _config(tmp_path, verify=['probe-tool check'], pass_env=['ONLY_HOST']),
        PATH=_path(tools),
        ONLY_HOST='1',
    )

    assert _line(hidden, 'exe:') == 'exe: ok probe-tool（版本未知：退出 3）'
    assert _line(passed, 'exe:') == 'exe: ok probe-tool（probe 1.0）'
    assert (hidden.exit_code, passed.exit_code) == (0, 0)


def test_python_is_probed_in_the_agent_environment(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(
        tools,
        'python3',
        '#!/bin/sh\n[ -n "$ONLY_HOST" ] || { echo "no host var" >&2; exit 3; }\n'
        'echo "Python 3.99.0"\n',
    )

    outcome = _doctor(tmp_path, root, _config(tmp_path), PATH=_path(tools), ONLY_HOST='1')

    assert _line(outcome, 'python:') == 'python: ok（版本未知：退出 3：no host var）'


def test_builtins_and_tools_without_version_flags_are_present_not_missing(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / 'sub').mkdir()
    verify = ['cd sub && true', 'env FOO=1 true', '[ -f README.md ]', 'dash -c true', 'uname -a']

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=verify), verify=True)

    assert outcome.exit_code == 0
    lines = _lines(outcome, 'exe:')
    assert lines[0] == 'exe: ok cd（shell 内建命令）'
    assert lines[1].startswith('exe: ok env（')
    assert lines[2] == 'exe: ok [（shell 内建命令）'
    assert lines[3].startswith('exe: ok dash（')
    assert lines[4].startswith('exe: ok uname（')
    assert all(line.startswith('verify: passed ') for line in _lines(outcome, 'verify:'))


@pytest.mark.parametrize(
    ('command', 'word'),
    [
        ('(cd . && true)', '(cd'),
        ('PATH="$PWD/bin:$PATH" mytool', 'mytool'),
        ('$TOOL run', '$TOOL'),
    ],
)
def test_words_doctor_cannot_resolve_without_running_them_are_unchecked(
    tmp_path: Path, command: str, word: str
):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=[command]))

    assert outcome.exit_code == 0
    assert _line(outcome, 'exe:').startswith(f'exe: unchecked {word}（')


def test_a_program_inside_the_workspace_is_never_run_by_plain_doctor(tmp_path: Path):
    root = _workspace(tmp_path)
    marker = tmp_path / 'ran'
    _tool(root, 'check.sh', f'#!/bin/sh\ntouch {marker}\necho check 1.0\n')

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['./check.sh']))

    assert _line(outcome, 'exe:') == (
        'exe: ok ./check.sh（在工作区里：./check.sh，doctor 不运行它）'
    )
    assert not marker.exists()
    assert outcome.exit_code == 0


def test_a_program_found_through_a_path_inside_the_workspace_is_not_run(tmp_path: Path):
    root = _workspace(tmp_path)
    marker = tmp_path / 'ran'
    _tool(root / 'bin', 'local-tool', f'#!/bin/sh\ntouch {marker}\necho local 1.0\n')

    outcome = _doctor(
        tmp_path, root, _config(tmp_path, verify=['local-tool x']), PATH=_path(root / 'bin')
    )

    assert '在工作区里' in _line(outcome, 'exe:')
    assert not marker.exists()


def test_the_executable_word_is_never_shell_evaluated(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['"touch pwned;true" x']))

    assert not (root / 'pwned').exists()
    assert _line(outcome, 'exe:').startswith('exe: unchecked touch pwned;true（')


def test_a_tool_in_a_directory_with_a_space_is_probed_correctly(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'my tools'
    _tool(tools, 'space-tool', '#!/bin/sh\necho "space 1.0"\n')

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['space-tool']), PATH=_path(tools))

    assert _line(outcome, 'exe:') == 'exe: ok space-tool（space 1.0）'


def test_a_word_both_checked_and_unchecked_is_listed_once(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['PATH="$PWD:$PATH" true', 'true']))

    assert _lines(outcome, 'exe:') == ['exe: ok true（shell 内建命令）']


def test_leading_assignments_are_skipped_and_each_executable_is_checked_once(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['FOO=1 BAR=2 true x', 'true y']))

    assert _lines(outcome, 'exe:') == ['exe: ok true（shell 内建命令）']


def test_an_assignment_only_command_has_no_executable(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['FOO=1']))

    assert _lines(outcome, 'exe:') == []
    assert outcome.exit_code == 0


def test_quotes_around_the_executable_are_removed(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=["'true' x"]))

    assert _lines(outcome, 'exe:') == ['exe: ok true（shell 内建命令）']


def test_executables_keep_their_first_appearance_order(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    for name in ('zz-tool', 'aa-tool', 'mm-tool'):
        _tool(tools, name, '#!/bin/sh\necho v\n')

    outcome = _doctor(
        tmp_path,
        root,
        _config(tmp_path, verify=['zz-tool', 'aa-tool', 'mm-tool']),
        PATH=_path(tools),
    )

    assert _lines(outcome, 'exe:') == [
        'exe: ok zz-tool（v）',
        'exe: ok aa-tool（v）',
        'exe: ok mm-tool（v）',
    ]


def test_a_verify_command_that_cannot_be_split_fails(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=["\x1b[2J 'open"]))

    assert outcome.exit_code == 1
    _no_raw_controls(outcome)
    assert "拆不开 verify 命令 `\\x1b[2J 'open`" in outcome.output


# ---- 版本文字 ----


def test_a_hostile_version_string_is_escaped(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'esc-tool', "#!/bin/sh\nprintf '\\033[2J1.0\\n'\n")

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['esc-tool']), PATH=_path(tools))

    _no_raw_controls(outcome)
    assert _lines(outcome, 'exe:') == ['exe: ok esc-tool（\\x1b[2J1.0）']


def test_an_unresolvable_name_with_control_characters_is_escaped(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['\x1b[31mred x']))

    _no_raw_controls(outcome)
    assert _line(outcome, 'exe:').startswith('exe: unchecked \\x1b[31mred（')


def test_a_long_version_is_cut_to_80_characters(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'long-tool', "#!/bin/sh\nprintf 'V%.0s' $(seq 200); echo\n")

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['long-tool']), PATH=_path(tools))

    assert _lines(outcome, 'exe:') == ['exe: ok long-tool（' + 'V' * 79 + '…）']


def test_the_stdout_version_wins_over_a_stderr_warning(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'warn-tool', '#!/bin/sh\necho "warning: cache" >&2\necho "warn-tool 3.1"\n')

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['warn-tool']), PATH=_path(tools))

    assert _lines(outcome, 'exe:') == ['exe: ok warn-tool（warn-tool 3.1）']


def test_a_version_printed_on_stderr_is_shown(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'old-tool', '#!/bin/sh\necho "old-tool 2.7" >&2\n')

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['old-tool run']), PATH=_path(tools))

    assert _line(outcome, 'exe:') == 'exe: ok old-tool（old-tool 2.7）'


def test_a_whitespace_only_first_line_is_skipped(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'ws-tool', "#!/bin/sh\nprintf '   \\nws-tool 2.0\\n'\n")

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['ws-tool']), PATH=_path(tools))

    assert _lines(outcome, 'exe:') == ['exe: ok ws-tool（ws-tool 2.0）']


def test_a_failing_version_probe_carries_its_stderr_detail(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'needy', '#!/bin/sh\necho "needy: set NEEDY_HOME" >&2\nexit 4\n')

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['needy']), PATH=_path(tools))

    assert _line(outcome, 'exe:') == 'exe: ok needy（版本未知：退出 4：needy: set NEEDY_HOME）'
    assert outcome.exit_code == 0


def test_a_hanging_version_probe_is_cut_off_and_only_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(host_module, '_PROBE_TIMEOUT', 1.0)
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'hang-tool', '#!/bin/sh\nexec sleep 20\n')
    _tool(tools, 'python3', '#!/bin/sh\nexec sleep 20\n')
    started = time.monotonic()

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['hang-tool']), PATH=_path(tools))

    assert time.monotonic() - started < 15
    assert _line(outcome, 'exe:') == 'exe: ok hang-tool（版本未知：1 秒内没有结束）'
    assert _line(outcome, 'python:') == 'python: ok（版本未知：1 秒内没有结束）'
    assert outcome.exit_code == 0


def test_every_probe_gets_the_30_second_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[float] = []
    original = host_module.LocalWorkspace.run

    async def spy(self: object, command: str, **options: Any) -> object:
        seen.append(options['timeout'])
        return await original(self, command, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(host_module.LocalWorkspace, 'run', spy)
    root = _workspace(tmp_path)

    _doctor(tmp_path, root, _config(tmp_path, verify=['true', 'uname']))

    assert seen and set(seen) == {30.0}


# ---- git ----


@pytest.mark.parametrize(
    ('git', 'changes', 'expected'),
    [
        (False, 0, 'git: 非 git 仓库'),
        (True, 0, 'git: clean'),
        (True, 1, 'git: 1 处改动'),
        (True, 2, 'git: 2 处改动'),
    ],
)
def test_the_git_line_describes_the_workspace(
    tmp_path: Path, git: bool, changes: int, expected: str
):
    root = _workspace(tmp_path, git=git)
    for index in range(changes):
        (root / f'new{index}.txt').write_text('x\n', encoding='utf-8')

    outcome = _doctor(tmp_path, root, _config(tmp_path))

    assert _line(outcome, 'git:') == expected
    assert outcome.exit_code == 0


def test_without_git_on_the_path_the_git_line_says_so(tmp_path: Path):
    root = _workspace(tmp_path, git=False)
    empty = tmp_path / 'empty-bin'
    empty.mkdir()

    outcome = _doctor(tmp_path, root, _config(tmp_path), PATH=str(empty))

    assert _line(outcome, 'git:') == 'git: 没有 git（宿主的 PATH 里找不到它）'
    assert _line(outcome, 'python:') == 'python: 没有（Agent 的环境里找不到 python3）'
    assert outcome.exit_code == 0


def test_a_git_that_is_too_old_is_not_called_not_a_repository(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(
        tools,
        'git',
        '#!/bin/sh\necho "error: unknown option \\`attr-source=HEAD\'" >&2\nexit 129\n',
    )

    outcome = _doctor(tmp_path, root, _config(tmp_path), PATH=_path(tools))

    line = _line(outcome, 'git:')
    assert line.startswith('git: 用不了（`git rev-parse` 退出 129：error: unknown option')
    assert '2.40 以上' in line


def test_a_git_that_cannot_be_executed_is_not_called_missing(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'git', '#!/bin/sh\nexit 0\n')
    (tools / 'git').chmod(0o644)

    outcome = _doctor(tmp_path, root, _config(tmp_path), PATH=str(tools))

    assert _line(outcome, 'git:').startswith('git: 用不了（')


def test_a_bare_repository_is_not_a_work_tree(tmp_path: Path):
    root = tmp_path / 'bare.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(root)], check=True, env=_environ(tmp_path))

    outcome = _doctor(tmp_path, root, _config(tmp_path))

    assert _line(outcome, 'git:') == 'git: 非 git 仓库'


def test_changes_that_cannot_be_listed_are_reported_not_called_clean(tmp_path: Path):
    root = _workspace(tmp_path)
    nested = root / 'vendor'
    nested.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=nested, check=True, env=_environ(tmp_path))
    (nested / 'x').write_text('x\n', encoding='utf-8')

    outcome = _doctor(tmp_path, root, _config(tmp_path))

    line = _line(outcome, 'git:')
    assert line.startswith('git: 列不出改动（') and 'vendor/' in line
    assert outcome.exit_code == 0


def test_the_git_line_uses_the_host_git_configuration(tmp_path: Path):
    root = _workspace(tmp_path)
    excludes = tmp_path / 'excludes'
    excludes.write_text('scratch.txt\n', encoding='utf-8')
    global_config = tmp_path / 'host-gitconfig'
    global_config.write_text(f'[core]\n\texcludesFile = {excludes}\n', encoding='utf-8')
    (root / 'scratch.txt').write_text('x\n', encoding='utf-8')

    outcome = _doctor(tmp_path, root, _config(tmp_path), GIT_CONFIG_GLOBAL=str(global_config))

    assert _line(outcome, 'git:') == 'git: clean'


# ---- --verify ----


def test_verify_runs_every_command_and_a_failure_carries_its_parsed_summary(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'pytest', _FAKE_PYTEST)

    outcome = _doctor(
        tmp_path,
        root,
        _config(tmp_path, verify=['true', 'pytest -q']),
        verify=True,
        PATH=_path(tools),
    )

    assert outcome.exit_code == 1
    assert _lines(outcome, 'verify:') == [
        'verify: passed true（passed）',
        'verify: failed pytest -q（passed=2 failed=1）',
    ]


def test_an_early_verify_failure_is_not_overwritten_by_a_later_pass(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(
        tmp_path, root, _config(tmp_path, verify=['test -e missing', 'true']), verify=True
    )

    assert outcome.exit_code == 1


def test_a_verify_command_cannot_forge_a_line(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(
        tmp_path, root, _config(tmp_path, verify=['true\nexe: ok forged']), verify=True
    )

    assert 'exe: ok forged' not in outcome.output.splitlines()
    assert _lines(outcome, 'verify:')[0].startswith('verify: failed true\\nexe: ok forged（')


def test_verify_runs_in_the_agent_environment(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(
        tmp_path,
        root,
        _config(tmp_path, verify=['test -z "$ONLY_HOST"']),
        verify=True,
        ONLY_HOST='1',
    )

    assert _lines(outcome, 'verify:') == ['verify: passed test -z "$ONLY_HOST"（passed）']


def test_a_verify_command_that_changes_files_fails_like_the_handoff_would(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(
        tmp_path, root, _config(tmp_path, verify=['touch generated.txt']), verify=True
    )

    assert outcome.exit_code == 1
    assert '改了工作区里的文件：generated.txt' in _lines(outcome, 'verify:')[-1]


def test_a_secret_in_a_verify_command_is_masked(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(
        tmp_path,
        root,
        _config(tmp_path, verify=[f'test -n {_SECRET}']),
        verify=True,
        SERVICE_TOKEN=_SECRET,
    )

    assert _SECRET not in outcome.output
    assert '[REDACTED:secret.env_value]' in outcome.output


def test_without_the_verify_flag_nothing_is_run(tmp_path: Path):
    root = _workspace(tmp_path)
    tools = tmp_path / 'bin'
    _tool(tools, 'pytest', _FAKE_PYTEST)

    outcome = _doctor(tmp_path, root, _config(tmp_path, verify=['pytest -q']), PATH=_path(tools))

    assert outcome.exit_code == 0
    assert 'verify:' not in outcome.output
    assert _line(outcome, 'exe:') == 'exe: ok pytest（pytest 8.0.0）'


def test_verify_without_commands_says_so(tmp_path: Path):
    root = _workspace(tmp_path)

    outcome = _doctor(tmp_path, root, _config(tmp_path), verify=True)

    assert outcome.exit_code == 0
    assert _line(outcome, 'verify:') == (
        'verify: not_configured（.syndica.yaml 的 workspace.verify 是空的）'
    )


# ---- CLI ----


def test_the_cli_exits_1_when_a_check_fails(tmp_path: Path):
    root = _workspace(tmp_path)
    config = _config(tmp_path, verify=['nosuchtool-xyz run'])
    stdout = StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO())
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=root,
        environ=_environ(tmp_path),
    )
    try:
        code = main(['--config', str(config), 'doctor'], host=host, io=io)
    finally:
        host.close()

    assert code == 1
    assert 'exe: failed nosuchtool-xyz' in stdout.getvalue()


def test_a_host_without_the_verify_parameter_still_runs_plain_doctor():
    class _OldHost:
        def doctor(self, config: str) -> str:
            return f'healthy {config}'

    stdout = StringIO()

    code = main(
        ['doctor'],
        host=_OldHost(),  # type: ignore[arg-type]
        io=CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO()),
    )

    assert code == 0
    assert stdout.getvalue() == 'healthy .syndica.yaml\n'


def test_the_module_entrypoint_routes_verify(tmp_path: Path):
    (tmp_path / '.syndica.yaml').write_text(
        'version: 1\nworkspace: {verify: ["test -e missing"]}\n', encoding='utf-8'
    )
    model_env = {
        'ANTHROPIC_API_KEY',
        'GEMINI_API_KEY',
        'OPENAI_API_KEY',
        'OPENAI_BASE_URL',
        'OPENAI_MODEL',
        'OPENROUTER_API_KEY',
        'OPENROUTER_MODEL',
        'SYNDICA_LLM_ORDER',
    }
    env = {key: value for key, value in _environ(tmp_path).items() if key not in model_env}
    env.update(
        {
            'OPENAI_BASE_URL': 'https://llm.example.test/v1',
            'OPENAI_API_KEY': 'test-key',
            'OPENAI_MODEL': 'm',
        }
    )

    done = subprocess.run(
        [sys.executable, '-m', 'syndica', 'doctor', '--verify'],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 1
    assert 'verify: failed test -e missing（exit 1）' in done.stdout.splitlines()
