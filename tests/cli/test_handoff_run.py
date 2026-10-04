"""`run --handoff` 端到端（spec D13）：经 CLI 宿主跑一次脚本化的 run，看交接文件、stdout、stderr
与退出码。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
import subprocess
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import syndica.cli.git_finish as git_finish
import syndica.cli.host as host_module
from syndica.cli.handoff import lock_workspace
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import ConfigError, LLMResponseError, ScriptedLLM, ScriptedTurn, ToolCall

_SECRET = 'Zq9' + 'Lm4Xw7Pk2Rt8Vb5Nc3Jd6'


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
        'SERVICE_TOKEN': _SECRET,
    }


def _git(root: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _workspace(tmp_path: Path, *, git: bool = True, name: str = 'ws') -> Path:
    root = tmp_path / name
    (root / 'src').mkdir(parents=True)
    (root / 'README.md').write_text('# project\n', encoding='utf-8')
    (root / 'src' / 'keep.py').write_text('KEEP = 1\n', encoding='utf-8')
    if git:
        env = _env(tmp_path)
        _git(root, env, 'init', '-q')
        _git(root, env, 'add', '.')
        _git(root, env, 'commit', '-q', '-m', 'init')
    return root


def _config(
    tmp_path: Path, verify: list[str] | None = None, protected: list[str] | None = None
) -> Path:
    (tmp_path / 'lead.md').write_text('You lead.\n', encoding='utf-8')
    (tmp_path / 'reviewer.md').write_text('You review.\n', encoding='utf-8')
    workspace = {}
    if verify is not None:
        workspace['verify'] = verify
    if protected is not None:
        workspace['protected'] = protected
    lines = [
        'version: 1',
        'agents:',
        '  - name: Reviewer',
        '    description: reviews',
        '    system_prompt: reviewer.md',
        '    tools: [Read, Bash]',
        '  - name: Lead',
        '    description: leads',
        '    system_prompt: lead.md',
        '    tools: [Write, Edit, Bash]',
        '    subagents: [Reviewer]',
    ]
    if workspace:
        lines.append(f'workspace: {json.dumps(workspace)}')
    path = tmp_path / 'syndica.yaml'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def _reviewed(verdict: str = 'pass', *, answer: str = 'done\ncommit_title: add new') -> ScriptedLLM:
    """Lead 写一个文件、派 Reviewer；Reviewer 给出结论；Lead 收尾。"""
    return ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'Write', path='src/new.py', content='NEW = 1\n')]),
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='review')]),
            ScriptedTurn(content=f'looks fine\nverdict: {verdict}'),
            ScriptedTurn(content=answer),
        ]
    )


class _Run:
    def __init__(self, code: int, stdout: str, stderr: str, root: Path) -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.root = root

    @property
    def files(self) -> list[Path]:
        directory = self.root / '.syndica' / 'handoffs'
        return sorted(directory.iterdir()) if directory.exists() else []

    @property
    def matter(self) -> dict[str, str]:
        head = self.stdout.split('---\n')[1]
        return dict(line.split(': ', 1) for line in head.splitlines())


def _run(
    tmp_path: Path,
    llm: ScriptedLLM,
    *,
    config: Path,
    root: Path,
    agent: str = 'Lead',
    stdin: str = '',
    extra: tuple[str, ...] = ('--handoff',),
) -> _Run:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=root,
        environ=_env(tmp_path),
    )
    try:
        code = main(
            ['--config', str(config), 'run', *extra, '--agent', agent, 'work on it'],
            host=host,
            io=io,
        )
    finally:
        host.close()
    return _Run(code, stdout.getvalue(), stderr.getvalue(), root)


def test_a_root_failing_on_a_syndica_error_is_one_whole_section_in_the_handoff(tmp_path: Path):
    root = _workspace(tmp_path)
    error = LLMResponseError(
        expected='模型服务返回一条 assistant 消息',
        received=f'HTTP 429 token={_SECRET} \x1b]0;pwned\x07 尾巴',
        next_action='换一个模型，或等额度恢复后重试',
    )

    run = _run(
        tmp_path, ScriptedLLM([ScriptedTurn(raises=error)]), config=_config(tmp_path), root=root
    )

    (path,) = run.files
    document = path.read_text(encoding='utf-8')
    section = document.split('run 没有正常结束：', 1)[1].split('## ', 1)[0]
    assert run.code == 1
    assert run.matter['status'] == 'run_failed'
    assert re.search(r'^> Agent Lead（[^）]+）以错误结束：$', section, flags=re.MULTILINE)
    assert [line for line in section.splitlines() if line.startswith('> 下一步')] == [
        '> 下一步: 换一个模型，或等额度恢复后重试'
    ]
    assert document.count('换一个模型，或等额度恢复后重试') == 1
    assert '以错误结束' not in run.stderr
    for stream in (run.stdout, run.stderr, document):
        assert _SECRET not in stream
        assert '\x1b' not in stream


def test_the_user_redaction_rules_apply_to_the_handoff_and_its_printout(tmp_path: Path):
    root = _workspace(tmp_path)
    config = _config(tmp_path, verify=['true'])
    with config.open('a', encoding='utf-8') as file:
        file.write(
            "redaction:\n  rules:\n    - {id: custom.host, pattern: 'corp-[a-z0-9]+\\.internal'}\n"
        )
    answer = 'done, deployed to corp-db7.internal\ncommit_title: add new'

    run = _run(tmp_path, _reviewed(answer=answer), config=config, root=root)

    (path,) = run.files
    document = path.read_text(encoding='utf-8')
    assert 'corp-db7.internal' not in document + run.stdout + run.stderr
    assert 'deployed to [REDACTED:custom.host]' in document


def test_a_topology_run_hands_off_like_a_single_agent_run(tmp_path: Path):
    root = _workspace(tmp_path)
    config = _config(tmp_path, verify=['true'])
    slot = {'name': 'root', 'agent': 'Lead', 'is_root': True, 'cardinality': 'singleton'}
    with config.open('a', encoding='utf-8') as file:
        file.write(f'topologies: {json.dumps({"dev": {"slots": [slot]}})}\n')
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(''), stdout=stdout, stderr=stderr)
    llm = _reviewed()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=root,
        environ=_env(tmp_path),
    )
    try:
        code = main(
            ['--config', str(config), 'run', '--handoff', '--topology', 'dev', 'work on it'],
            host=host,
            io=io,
        )
    finally:
        host.close()
    run = _Run(code, stdout.getvalue(), stderr.getvalue(), root)

    assert run.code == 0
    assert run.matter['status'] == 'verified'
    assert (run.matter['host_verify'], run.matter['reviewer_verdict']) == ('passed', 'pass')
    assert '- `??` src/new.py\n' in run.stdout
    (path,) = run.files
    assert path.read_text(encoding='utf-8') == run.stdout


def test_a_topology_root_that_runs_out_of_turns_is_a_failed_run(tmp_path: Path):
    root = _workspace(tmp_path)
    config = _config(tmp_path, verify=['true'])
    slot = {'name': 'root', 'agent': 'Lead', 'is_root': True, 'cardinality': 'singleton'}
    with config.open('a', encoding='utf-8') as file:
        file.write(f'topologies: {json.dumps({"dev": {"slots": [slot]}})}\n')
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'Write', path='src/new.py', content='NEW = 1\n')]),
            ScriptedTurn(content='half done', tool_calls=[_call('b', 'Bash', command='true')]),
        ]
    )
    stdout = StringIO()
    io = CliIO(stdin=StringIO(''), stdout=stdout, stderr=StringIO())
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=root,
        environ=_env(tmp_path),
    )
    try:
        code = main(
            [
                '--config',
                str(config),
                'run',
                '--handoff',
                '--topology',
                'dev',
                '--max-turns',
                '2',
                'work on it',
            ],
            host=host,
            io=io,
        )
    finally:
        host.close()
    run = _Run(code, stdout.getvalue(), '', root)

    assert run.code == 1
    assert (run.matter['status'], run.matter['host_verify']) == ('run_failed', 'skipped')
    assert '根 Agent 以 max_turns 结束' in run.stdout


def test_a_verified_run_prints_the_handoff_once_and_writes_the_same_file(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    (path,) = run.files
    assert run.code == 0
    assert run.matter == {
        'schema': 'syndica.handoff/1',
        'status': 'verified',
        'host_verify': 'passed',
        'reviewer_verdict': 'pass',
        'git': 'declined',
        'protected_changed': '[]',
        'exit_code': '0',
    }
    assert list(run.matter) == [
        'schema',
        'status',
        'host_verify',
        'reviewer_verdict',
        'git',
        'protected_changed',
        'exit_code',
    ]
    assert path.read_text(encoding='utf-8') == run.stdout
    assert run.stdout.count('schema: syndica.handoff/1') == 1
    assert f'交接文件：{path}' in run.stderr
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (root / '.syndica' / '.gitignore').read_text(encoding='utf-8') == '*\n'
    assert '- `??` src/new.py\n' in run.stdout


def test_a_failing_verify_command_fails_verification_and_exits_1(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true', 'false']), root=root)

    assert run.code == 1
    assert (run.matter['status'], run.matter['host_verify']) == ('verification_failed', 'failed')
    assert '- 失败：`false`' in run.stdout
    assert '修好复核命令 `false`' in run.stdout


def test_without_verify_the_run_is_unverified_and_exits_0(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path), root=root)

    assert run.code == 0
    assert (run.matter['status'], run.matter['host_verify']) == ('unverified', 'not_configured')
    assert 'workspace.verify 里配上' in run.stdout


def test_a_passing_verify_with_a_blocking_reviewer_is_unverified(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _reviewed('block'), config=_config(tmp_path, verify=['true']), root=root)

    assert run.code == 0
    assert (run.matter['status'], run.matter['reviewer_verdict']) == ('unverified', 'block')
    assert '人工 review' in run.stdout


def test_a_failed_run_skips_verification_and_exits_1(tmp_path: Path):
    root = _workspace(tmp_path)
    marker = tmp_path / 'verify-ran'

    run = _run(
        tmp_path,
        ScriptedLLM([]),
        config=_config(tmp_path, verify=[f'touch {marker}']),
        root=root,
    )

    assert run.code == 1
    assert (run.matter['status'], run.matter['host_verify']) == ('run_failed', 'skipped')
    assert not marker.exists()
    assert 'run 没有正常结束' in run.stdout


def test_a_verify_command_that_changes_files_fails_verification_and_names_them(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(
        tmp_path, _reviewed(), config=_config(tmp_path, verify=['touch generated.txt']), root=root
    )

    assert run.code == 1
    assert run.matter['status'] == 'verification_failed'
    assert '复核命令改了这些文件，复核按失败算：\n- generated.txt' in run.stdout


def test_outside_git_the_handoff_says_why_it_cannot_list_changes(tmp_path: Path):
    root = _workspace(tmp_path, git=False)
    env = _env(tmp_path)
    env['GIT_CEILING_DIRECTORIES'] = str(tmp_path)

    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=stderr)
    llm = _reviewed()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE, io=io, model_factory=lambda _: llm, workspace_root=root, environ=env
    )
    try:
        code = main(
            [
                '--config',
                str(_config(tmp_path, verify=['true'])),
                'run',
                '--handoff',
                '--agent',
                'Lead',
                'go',
            ],
            host=host,
            io=io,
        )
    finally:
        host.close()

    assert code == 0
    assert '列不出改动：' in stdout.getvalue()
    assert 'reviewer_verdict: unverified' in stdout.getvalue()


def test_a_file_changed_before_the_run_and_again_during_it_is_called_out(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / 'src' / 'keep.py').write_text('KEEP = 2  # mine\n', encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[_call('w', 'Write', path='src/keep.py', content='KEEP = 3\n')]
            ),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert '运行前已经改过、这次又被改的文件（提交前请人核对）：\n- src/keep.py' in run.stdout


def test_an_interruption_before_the_final_rewrite_leaves_the_file_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)

    def interrupted(*args: object) -> None:
        raise RuntimeError('interrupted before the final rewrite')

    monkeypatch.setattr(host_module, 'rewrite_handoff', interrupted)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    (path,) = run.files
    text = path.read_text(encoding='utf-8')
    assert run.code == 1
    assert 'git: pending\n' in text
    assert 'exit_code: null\n' in text
    assert run.stdout == ''


def _press() -> None:
    """按一次 Ctrl-C：直接调宿主装上的处理，与真的 SIGINT 走同一段代码，却不会打断 pytest。"""
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler) and handler is not signal.default_int_handler
    handler(signal.SIGINT, None)


def _pressed_after(original: Any) -> Any:
    """原样做完 `original`，在它最后一次 await 之后的同步代码里按一次 Ctrl-C。"""

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        _press()
        return result

    return wrapped


def test_a_ctrl_c_at_the_end_of_the_run_still_hands_off_and_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    original = RuntimeCliHost._run_request  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(RuntimeCliHost, '_run_request', _pressed_after(original))

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    (path,) = run.files
    assert run.code == 130
    assert (run.matter['status'], run.matter['host_verify'], run.matter['git']) == (
        'run_failed',
        'skipped',
        'not_offered',
    )
    assert '被中断（Ctrl-C）：run 没有跑完' in run.stdout
    assert path.read_text(encoding='utf-8') == run.stdout


def test_a_ctrl_c_at_the_end_of_verification_skips_it_and_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    original = RuntimeCliHost._verify  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(RuntimeCliHost, '_verify', _pressed_after(original))

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    assert run.code == 130
    assert (run.matter['status'], run.matter['host_verify']) == ('run_failed', 'skipped')
    assert '被中断（Ctrl-C）：宿主复核没有跑完。' in run.stdout
    assert '没有复核：' in run.stdout and '- 通过：`true`' not in run.stdout


def _pressed_once_finished(original: Any) -> Any:
    """原样做完 `original`，在它的任务结束之后、等它的宿主醒来之前按一次 Ctrl-C。"""

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        asyncio.get_running_loop().call_soon(_press)
        return result

    return wrapped


def test_a_ctrl_c_just_after_verification_finished_drops_its_results_and_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    original = RuntimeCliHost._verify  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(RuntimeCliHost, '_verify', _pressed_once_finished(original))

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    assert run.code == 130
    assert (run.matter['status'], run.matter['host_verify']) == ('run_failed', 'skipped')
    assert '被中断（Ctrl-C）：宿主复核没有跑完。' in run.stdout
    assert '没有复核：' in run.stdout and '- 通过：`true`' not in run.stdout


def test_a_ctrl_c_while_the_handoff_is_written_skips_the_git_step_and_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    head = _git(root, _env(tmp_path), 'rev-parse', 'HEAD')
    original = host_module.write_handoff

    def write_then_interrupted(*args: Any, **kwargs: Any) -> Any:
        written = original(*args, **kwargs)
        _press()
        return written

    monkeypatch.setattr(host_module, 'write_handoff', write_then_interrupted)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    (path,) = run.files
    assert run.code == 130
    assert (run.matter['git'], run.matter['exit_code']) == ('not_offered', '130')
    assert '被中断（Ctrl-C），不做 git 收尾' in run.stdout
    assert path.read_text(encoding='utf-8') == run.stdout
    assert _git(root, _env(tmp_path), 'rev-parse', 'HEAD') == head


def test_an_interrupted_run_whose_final_rewrite_fails_still_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    original = RuntimeCliHost._run_request  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(RuntimeCliHost, '_run_request', _pressed_after(original))

    def failing(*args: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(host_module, 'rewrite_handoff', failing)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    assert run.code == 130
    assert run.matter['exit_code'] == '130'
    assert '交接文件没能更新成这一版' in run.stdout


def test_a_ctrl_c_after_the_git_step_leaves_the_outcome_and_its_exit_code_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    original = host_module.rewrite_handoff

    def interrupted_then_rewritten(*args: Any, **kwargs: Any) -> Any:
        _press()
        _press()
        return original(*args, **kwargs)

    monkeypatch.setattr(host_module, 'rewrite_handoff', interrupted_then_rewritten)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    (path,) = run.files
    assert (run.code, run.matter['exit_code'], run.matter['git']) == (0, '0', 'declined')
    assert path.read_text(encoding='utf-8') == run.stdout
    assert '被中断' not in run.stderr


def test_a_ctrl_c_after_a_read_only_run_is_settled_leaves_its_outcome_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    original = host_module.rewrite_handoff

    def interrupted_then_rewritten(*args: Any, **kwargs: Any) -> Any:
        _press()
        return original(*args, **kwargs)

    monkeypatch.setattr(host_module, 'rewrite_handoff', interrupted_then_rewritten)

    run = _run(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='nothing to change')]),
        config=_config(tmp_path),
        root=root,
        extra=('--handoff', '--no-write'),
    )

    assert (run.code, run.matter['exit_code']) == (0, '0')
    assert '被中断' not in run.stderr


def test_a_ctrl_c_just_before_the_git_step_skips_it_without_saying_it_was_cut_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    head = _git(root, _env(tmp_path), 'rev-parse', 'HEAD')
    written: list[bool] = []
    original_write = host_module.write_handoff
    original_environment = host_module.agent_environment

    def write(*args: Any, **kwargs: Any) -> Any:
        path = original_write(*args, **kwargs)
        written.append(True)
        return path

    def pressed_once_written(*args: Any, **kwargs: Any) -> Any:
        if written:
            _press()
        return original_environment(*args, **kwargs)

    monkeypatch.setattr(host_module, 'write_handoff', write)
    monkeypatch.setattr(host_module, 'agent_environment', pressed_once_written)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    (path,) = run.files
    assert run.code == 130
    assert (run.matter['git'], run.matter['exit_code']) == ('not_offered', '130')
    assert '被中断（Ctrl-C），不做 git 收尾' in run.stdout
    assert 'git 收尾被中断' not in run.stderr
    assert path.read_text(encoding='utf-8') == run.stdout
    assert _git(root, _env(tmp_path), 'rev-parse', 'HEAD') == head


def test_presses_while_an_interrupted_commit_unwinds_do_not_stop_the_unstaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    head = _git(root, _env(tmp_path), 'rev-parse', 'HEAD')
    real_hooked = git_finish._hooked  # pyright: ignore[reportPrivateUsage]

    def hooked(*args: Any, **kwargs: Any) -> Any:
        if args[3][0] == 'commit':
            _press()
        if args[3][0] == 'rm':
            _press()
            _press()
        return real_hooked(*args, **kwargs)

    monkeypatch.setattr(git_finish, '_hooked', hooked)

    run = _run(
        tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root, stdin='2\n'
    )

    assert run.code == 130
    assert run.matter['exit_code'] == '130'
    assert '宿主看到的：没有提交；改动还在工作区里；为提交暂存的新文件已经撤回。' in run.stdout
    assert run.stderr.count('git 收尾正在停下') == 1
    assert _git(root, _env(tmp_path), 'diff', '--cached', '--name-only') == ''
    assert _git(root, _env(tmp_path), 'rev-parse', 'HEAD') == head


def test_presses_after_the_outcome_is_settled_never_force_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    original_run = RuntimeCliHost._run_request  # pyright: ignore[reportPrivateUsage]
    original_rewrite = host_module.rewrite_handoff

    async def pressed_twice(*args: Any, **kwargs: Any) -> Any:
        result = await original_run(*args, **kwargs)
        _press()
        _press()
        return result

    def pressed_then_rewritten(*args: Any, **kwargs: Any) -> Any:
        _press()
        return original_rewrite(*args, **kwargs)

    monkeypatch.setattr(RuntimeCliHost, '_run_request', pressed_twice)
    monkeypatch.setattr(host_module, 'rewrite_handoff', pressed_then_rewritten)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    (path,) = run.files
    assert run.code == 130
    assert (run.matter['git'], run.matter['exit_code']) == ('not_offered', '130')
    assert path.read_text(encoding='utf-8') == run.stdout


def test_a_ctrl_c_the_run_turns_into_another_error_still_counts_as_an_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)

    async def interrupted_then_failed(*args: object, **kwargs: object) -> str:
        _press()
        raise RuntimeError('the run gave up when it was cancelled')

    monkeypatch.setattr(RuntimeCliHost, '_run_request', interrupted_then_failed)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    assert run.code == 130
    assert '被中断（Ctrl-C）：run 没有跑完' in run.stdout


def test_a_ctrl_c_verification_turns_into_another_error_still_counts_as_an_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)

    async def interrupted_then_failed(*args: object, **kwargs: object) -> object:
        _press()
        raise RuntimeError('verification gave up when it was cancelled')

    monkeypatch.setattr(RuntimeCliHost, '_verify', interrupted_then_failed)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    assert run.code == 130
    assert '被中断（Ctrl-C）：宿主复核没有跑完。' in run.stdout


def test_a_cancellation_that_is_not_a_ctrl_c_is_not_mistaken_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)

    async def cancelled(*args: object, **kwargs: object) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(RuntimeCliHost, '_run_request', cancelled)

    with pytest.raises(asyncio.CancelledError):
        _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)


def test_a_failure_after_the_writers_are_removed_is_still_redacted_and_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)

    def failing(*args: object) -> None:
        raise RuntimeError(f'could not rename, token {_SECRET} \u202e')

    monkeypatch.setattr(host_module, 'rewrite_handoff', failing)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path), root=root)

    assert run.code == 1
    assert _SECRET not in run.stderr
    assert '\u202e' not in run.stderr
    assert '[REDACTED:secret.env_value]' in run.stderr


def _asking(question: str) -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('q', 'AskUser', question=question)]),
            ScriptedTurn(content='done'),
        ]
    )


def test_under_handoff_questions_go_to_stderr_and_stdout_has_only_the_handoff(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _asking('Which option?'), config=_config(tmp_path), root=root, stdin='A\n')

    assert 'Which option?' in run.stderr
    assert 'Which option?' not in run.stdout
    assert run.stdout.startswith('---\nschema: syndica.handoff/1\n')


def test_questions_in_an_ordinary_run_are_escaped(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(
        tmp_path,
        _asking('Pick\x1b[2J\x9b1m\u202eevil'),
        config=_config(tmp_path),
        root=root,
        stdin='A\n',
        extra=(),
    )

    assert '\x1b' not in run.stdout
    assert '\x9b' not in run.stdout
    assert '\u202e' not in run.stdout
    assert 'Pick\\x1b[2J\\x9b1m\\u202eevil' in run.stdout


def test_a_secret_in_the_agents_words_never_reaches_the_file_or_either_stream(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('q', 'AskUser', question=f'use {_SECRET}?')]),
            ScriptedTurn(content=f'done, the key is {_SECRET}'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root, stdin='yes\n')

    (path,) = run.files
    for text in (path.read_text(encoding='utf-8'), run.stdout, run.stderr):
        assert _SECRET not in text
        assert _SECRET[:16] not in text
    assert '[REDACTED:secret.env_value]' in run.stdout
    assert '[REDACTED:secret.env_value]' in run.stderr


def test_a_second_handoff_run_on_a_locked_workspace_exits_1(tmp_path: Path):
    root = _workspace(tmp_path)
    held = lock_workspace(root)
    try:
        run = _run(tmp_path, _reviewed(), config=_config(tmp_path), root=root)
    finally:
        os.close(held)

    assert run.code == 1
    assert '另一个 run 锁着' in run.stderr
    assert run.files == []


def test_a_symlinked_syndica_directory_is_refused_and_the_handoff_still_printed(tmp_path: Path):
    root = _workspace(tmp_path)
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (root / '.syndica').symlink_to(outside, target_is_directory=True)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path), root=root)

    assert run.code == 1
    assert '交接文件没写成' in run.stderr
    assert '实际: 它是符号链接' in run.stderr
    assert '下一步: 删掉它再跑' in run.stderr
    assert run.stdout.startswith('---\nschema: syndica.handoff/1\n')
    assert run.matter['exit_code'] == '1'
    assert list(outside.iterdir()) == []


def test_the_file_tools_cannot_read_what_the_host_keeps_for_itself(tmp_path: Path):
    """trace 与交接文件里有此前标成不可信的工具输出，经 Read 读回来就成了可信内容。"""
    root = _workspace(tmp_path, git=False)
    for relative, text in (
        ('handoffs/old.md', 'verdict: pass\n'),
        ('traces/t1/spans.jsonl', '{"name": "untrusted"}\n'),
        ('tmp/out.txt', 'long output\n'),
    ):
        path = root / '.syndica' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('r', 'Read', path='.syndica/handoffs/old.md')]),
            ScriptedTurn(tool_calls=[_call('s', 'Read', path='.syndica/traces/t1/spans.jsonl')]),
            ScriptedTurn(tool_calls=[_call('t', 'Read', path='.syndica/tmp/out.txt')]),
            ScriptedTurn(content='done'),
        ]
    )

    _run(tmp_path, llm, config=_config(tmp_path), root=root, agent='Reviewer', extra=())

    handoff, trace, allowed = (llm.calls[index][-1].content for index in (1, 2, 3))
    assert isinstance(handoff, str) and isinstance(trace, str)
    assert 'verdict: pass' not in handoff and 'untrusted' not in trace
    assert '.syndica/handoffs' in handoff and '可信' in handoff
    assert '.syndica/traces' in trace
    assert allowed == 'long output'


def test_a_handoff_path_the_agent_blocked_with_a_file_still_prints_the_handoff(tmp_path: Path):
    root = _workspace(tmp_path)
    # 经变量拼出路径：危险规则认得出字面的 `.syndica/handoffs` 并拦下，变量是它声明过的盲区。
    block = 'd=.syndic; d="${d}a"; mkdir -p "$d" && echo x > "$d/handoffs"'
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('b', 'Bash', command=block)]),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert run.code == 1
    assert '实际: 它不是目录' in run.stderr
    assert run.stdout.startswith('---\nschema: syndica.handoff/1\n')


@pytest.mark.skipif(os.geteuid() == 0, reason='root 不受文件权限约束')
def test_a_verify_that_breaks_the_after_snapshot_fails_verification(tmp_path: Path):
    root = _workspace(tmp_path)
    command = "sh -c 'echo tampered >> src/keep.py; : > .locked; chmod 000 .locked'"

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=[command]), root=root)

    assert run.code == 1
    assert (run.matter['status'], run.matter['host_verify']) == ('verification_failed', 'failed')
    assert '列不出复核前后的改动' in run.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason='root 不受文件权限约束')
def test_a_verification_whose_before_snapshot_failed_does_not_pass(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('b', 'Bash', command=': > .locked; chmod 000 .locked')]),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path, verify=['true']), root=root)

    assert (run.code, run.matter['host_verify']) == (1, 'failed')
    assert '列不出复核前后的改动' in run.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason='root 不受文件权限约束')
def test_a_verification_that_only_has_an_after_snapshot_does_not_pass(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('b', 'Bash', command=': > .locked; chmod 000 .locked')]),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path, verify=['rm -f .locked']), root=root)

    assert (run.code, run.matter['host_verify']) == (1, 'failed')
    assert '列不出复核前后的改动' in run.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason='root 不受文件权限约束')
def test_a_pre_run_snapshot_that_failed_is_stated(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / '.locked').write_text('x\n', encoding='utf-8')
    (root / '.locked').chmod(0)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('b', 'Bash', command='rm -f .locked')]),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert '运行前的快照没取到，确认不了这次有没有改到人在运行前就改过的文件' in run.stdout


def test_a_verification_that_crashes_is_reported_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)

    async def crashed(self: object, config: object) -> tuple[object, ...]:
        raise RuntimeError('verify blew up')

    monkeypatch.setattr(RuntimeCliHost, '_verify', crashed)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    assert run.code == 1
    assert run.matter['status'] == 'verification_failed'
    assert '复核命令没能跑完：RuntimeError: verify blew up' in run.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason='root 不受文件权限约束')
def test_log_records_are_escaped_and_redacted_and_never_reach_the_real_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root = _workspace(tmp_path)
    locked = root / f'leak-{_SECRET}-\u202e.txt'
    locked.write_text('x\n', encoding='utf-8')
    locked.chmod(0)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['true']), root=root)

    real = capsys.readouterr().err
    assert _SECRET not in real
    assert '\u202e' not in real
    assert 'Reviewer' in run.stderr
    assert _SECRET not in run.stderr
    assert '\u202e' not in run.stderr


def test_the_handoff_path_line_is_redacted_and_escaped(tmp_path: Path):
    root = _workspace(tmp_path, name=f'ws-{_SECRET[:20]}-\u202e')

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path), root=root)

    assert '交接文件：' in run.stderr
    assert _SECRET[:20] not in run.stderr
    assert '\u202e' not in run.stderr
    assert '-\\u202e' in run.stderr


def test_the_answer_of_an_ordinary_run_is_escaped(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM([ScriptedTurn(content='done\x1b]0;pwned\x07\u202e')])

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root, extra=())

    assert run.code == 0
    assert '\x1b' not in run.stdout
    assert 'done\\x1b]0;pwned\\x07\\u202e' in run.stdout


def test_a_failure_printed_by_the_cli_is_escaped():
    class Failing:
        def run_request(self, request: object) -> str:
            raise RuntimeError('provider said \x1b[2J\u202egone')

    stderr = StringIO()
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr)

    code = main(['run', 'go'], host=Failing(), io=io)  # type: ignore[arg-type]

    assert code == 1
    assert '\x1b' not in stderr.getvalue()
    assert 'provider said \\x1b[2J\\u202egone' in stderr.getvalue()


def test_protected_paths_the_run_changed_are_listed_in_the_front_matter(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call('g', 'Write', path='.gitignore', content='*.log\n'),
                    _call('p', 'Write', path='pyproject.toml', content='[project]\n'),
                    _call('n', 'Write', path='src/free.py', content='x = 1\n'),
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path, protected=['pyproject.toml']), root=root)

    assert run.matter['protected_changed'] == '[".gitignore", "pyproject.toml"]'


def test_a_reviewer_that_changes_files_with_bash_does_not_count(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'Write', path='src/new.py', content='NEW = 1\n')]),
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='review')]),
            ScriptedTurn(tool_calls=[_call('b', 'Bash', command='echo sneaky >> src/new.py')]),
            ScriptedTurn(content='verdict: pass'),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path, verify=['true']), root=root)

    assert (run.matter['status'], run.matter['reviewer_verdict']) == ('unverified', 'unverified')


def test_a_read_only_handoff_is_read_only_and_skips_verification(tmp_path: Path):
    root = _workspace(tmp_path)
    marker = tmp_path / 'verify-ran'
    llm = ScriptedLLM([ScriptedTurn(content='nothing to change')])

    run = _run(
        tmp_path,
        llm,
        config=_config(tmp_path, verify=[f'touch {marker}']),
        root=root,
        agent='Reviewer',
        extra=('--handoff', '--no-write'),
    )

    assert run.code == 0
    assert (run.matter['status'], run.matter['host_verify']) == ('read_only', 'skipped')
    assert not marker.exists()


def test_the_checks_the_agent_ran_are_listed(tmp_path: Path):
    root = _workspace(tmp_path)
    tool = tmp_path / 'bin'
    tool.mkdir()
    fake = tool / 'pytest'
    fake.write_text('#!/bin/sh\necho "4 passed in 0.10s"\n', encoding='utf-8')
    fake.chmod(0o755)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('t', 'Bash', command=f'{fake} -q')]),
            ScriptedTurn(tool_calls=[_call('e', 'Bash', command='echo hello')]),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert f'`{fake} -q`：passed（passed=4）' in run.stdout
    assert 'echo hello' not in run.stdout


def test_a_verify_run_of_pytest_that_prints_no_summary_does_not_pass(tmp_path: Path):
    root = _workspace(tmp_path)
    tool = tmp_path / 'bin'
    tool.mkdir()
    fake = tool / 'pytest'
    fake.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    fake.chmod(0o755)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=[f'{fake} -q']), root=root)

    assert run.code == 1
    assert run.matter['host_verify'] == 'failed'


def test_the_host_refuses_a_handoff_for_a_session_on_its_own(tmp_path: Path):
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=_workspace(tmp_path),
        environ=_env(tmp_path),
    )
    try:
        with pytest.raises(ConfigError) as caught:
            host.run_request(RunRequest(prompt='go', session='agent-1', handoff=True))
    finally:
        host.close()

    assert caught.value.expected == '--handoff 用于一次新的 run'
    assert '去掉 --session' in caught.value.next_action


def _fake_pytest(tmp_path: Path, script: str) -> Path:
    tool = tmp_path / 'fakebin'
    tool.mkdir(exist_ok=True)
    fake = tool / 'pytest'
    fake.write_text(f'#!/bin/sh\n{script}\n', encoding='utf-8')
    fake.chmod(0o755)
    return fake


def test_a_verify_run_of_pytest_that_collected_no_tests_does_not_pass(tmp_path: Path):
    root = _workspace(tmp_path)
    fake = _fake_pytest(tmp_path, 'echo "no tests ran in 0.01s"\nexit 5')

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=[f'{fake} -q']), root=root)

    assert (run.code, run.matter['host_verify']) == (1, 'failed')


def test_a_verify_command_that_times_out_does_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _workspace(tmp_path)
    monkeypatch.setattr(host_module, '_VERIFY_TIMEOUT', 0.5)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=['sleep 5']), root=root)

    assert (run.code, run.matter['host_verify']) == (1, 'failed')


def test_a_model_that_fails_outright_still_leaves_a_run_failed_handoff(tmp_path: Path):
    root = _workspace(tmp_path)
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=stderr)

    def broken(name: str) -> ScriptedLLM:
        raise RuntimeError('provider down \x1b]0;pwned\x07')

    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=broken,
        workspace_root=root,
        environ=_env(tmp_path),
    )
    try:
        code = main(
            ['--config', str(_config(tmp_path)), 'run', '--handoff', '--agent', 'Lead', 'go'],
            host=host,
            io=io,
        )
    finally:
        host.close()

    assert code == 1
    assert 'status: run_failed' in stdout.getvalue()
    assert '\x1b' not in stdout.getvalue()
    assert '原因写在「Agent 的结论」一节开头' in stdout.getvalue()


def test_the_config_loaded_at_the_start_is_the_one_used_for_verify_and_protected(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / 'lead.md').write_text('You lead.\n', encoding='utf-8')
    (root / 'reviewer.md').write_text('You review.\n', encoding='utf-8')
    original = _config(tmp_path, verify=['false'], protected=['src/keep.py']).read_text(
        encoding='utf-8'
    )
    config = root / '.syndica.yaml'
    config.write_text(original, encoding='utf-8')
    forged = original.replace('"verify": ["false"]', '"verify": ["true"]').replace(
        '"protected": ["src/keep.py"]', '"protected": []'
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call('c', 'Write', path='.syndica.yaml', content=forged),
                    _call('k', 'Write', path='src/keep.py', content='KEEP = 9\n'),
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=config, root=root)

    assert run.matter['host_verify'] == 'failed'
    assert 'src/keep.py' in run.matter['protected_changed']


def test_a_human_edit_the_agent_reverted_is_called_out(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / 'src' / 'keep.py').write_text('KEEP = 2  # mine\n', encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[_call('w', 'Write', path='src/keep.py', content='KEEP = 1\n')]
            ),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert '运行前已经改过、这次又被改的文件（提交前请人核对）：\n- src/keep.py' in run.stdout


@pytest.mark.parametrize(
    'command', ['rm -f src/new.py', 'git add src/new.py', 'chmod +x src/new.py']
)
def test_a_verify_command_that_deletes_stages_or_chmods_a_file_fails(tmp_path: Path, command: str):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path, verify=[command]), root=root)

    assert (run.code, run.matter['host_verify']) == (1, 'failed')
    assert f'- 通过：`{command}`' in run.stdout
    assert '复核命令改了这些文件，复核按失败算：\n- src/new.py' in run.stdout


def test_a_change_made_with_edit_is_marked_as_produced_by_write_or_edit(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call(
                        'e',
                        'Edit',
                        path='src/keep.py',
                        old_string='KEEP = 1',
                        new_string='KEEP = 3',
                    )
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert '- ` M` src/keep.py\n' in run.stdout


def test_a_write_through_a_symlinked_directory_or_another_case_counts_as_written(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / 'alias').symlink_to('src', target_is_directory=True)
    env = _env(tmp_path)
    _git(root, env, 'add', 'alias')
    _git(root, env, 'commit', '-q', '-m', 'alias')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call('w', 'Write', path='alias/viaLink.py', content='x\n'),
                    _call('c', 'Write', path='SRC/Cafe\u0301.py', content='y\n'),
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert '- `??` src/viaLink.py\n' in run.stdout
    assert '不是 Write / Edit 产生的' not in run.stdout


def test_escape_sequences_in_checks_verify_commands_and_new_file_names_are_escaped(
    tmp_path: Path,
):
    root = _workspace(tmp_path)
    fake = _fake_pytest(tmp_path, 'echo "4 passed in 0.10s"')
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('t', 'Bash', command=f'{fake} -q "\x1b[31m"')]),
            ScriptedTurn(content='done'),
        ]
    )
    verify = ['touch "$(printf \'evil\\033[31m.txt\')"  # \x1b[32m']

    run = _run(tmp_path, llm, config=_config(tmp_path, verify=verify), root=root)

    assert '\x1b' not in run.stdout
    assert 'evil\\x1b[31m.txt' in run.stdout
    assert '# \\x1b[32m' in run.stdout


def test_the_handoff_file_name_carries_the_utc_time(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _reviewed(), config=_config(tmp_path), root=root)

    (path,) = run.files
    stamp = datetime.strptime(path.name.split('-')[0], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 120


def test_a_failed_run_without_verify_is_skipped_not_not_configured(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, ScriptedLLM([]), config=_config(tmp_path), root=root)

    assert (run.matter['status'], run.matter['host_verify']) == ('run_failed', 'skipped')


def test_verification_runs_in_the_agents_environment_not_the_hosts(tmp_path: Path):
    root = _workspace(tmp_path)
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=stderr)
    llm = _reviewed()
    environ = {**_env(tmp_path), 'DEPLOY_REGION': 'host-only'}
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=root,
        environ=environ,
    )
    config = _config(tmp_path, verify=['test -n "$DEPLOY_REGION"'])
    try:
        code = main(
            ['--config', str(config), 'run', '--handoff', '--agent', 'Lead', 'go'],
            host=host,
            io=io,
        )
    finally:
        host.close()

    assert code == 1
    assert 'host_verify: failed' in stdout.getvalue()


def test_a_root_that_runs_out_of_turns_is_a_failed_run_even_with_text(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='working on it',
                tool_calls=[_call('w', 'Write', path='src/a.py', content='a\n')],
            ),
            ScriptedTurn(
                content='still working',
                tool_calls=[_call('x', 'Write', path='src/b.py', content='b\n')],
            ),
        ]
    )

    run = _run(
        tmp_path,
        llm,
        config=_config(tmp_path, verify=['true']),
        root=root,
        extra=('--handoff', '--max-turns', '2'),
    )

    assert (run.code, run.matter['status']) == (1, 'run_failed')
    assert '以 max_turns 结束' in run.stdout


def test_a_root_aborted_by_end_of_input_is_a_failed_run(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(tmp_path, _asking('Go ahead?'), config=_config(tmp_path), root=root, stdin='')

    assert (run.code, run.matter['status']) == (1, 'run_failed')


def test_errors_of_child_agents_are_listed_in_the_handoff(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='review')]),
            ScriptedTurn(raises=RuntimeError('reviewer model crashed')),
            ScriptedTurn(content='done anyway'),
        ]
    )

    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert '## 运行中的错误' in run.stdout
    assert 'reviewer model crashed' in run.stdout


def test_protected_paths_changed_by_a_human_before_the_run_are_not_reported(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / 'pyproject.toml').write_text('[project]\n', encoding='utf-8')

    run = _run(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='looked around')]),
        config=_config(tmp_path, protected=['pyproject.toml']),
        root=root,
        agent='Reviewer',
        extra=('--handoff', '--no-write'),
    )

    assert run.matter['protected_changed'] == '[]'


def test_a_verified_run_without_changes_says_there_is_nothing_to_commit(tmp_path: Path):
    root = _workspace(tmp_path)

    run = _run(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='verdict: pass')]),
        config=_config(tmp_path, verify=['true']),
        root=root,
        agent='Reviewer',
    )

    assert run.matter['status'] == 'verified'
    assert '不需要提交' in run.stdout


def test_handoff_and_session_together_are_refused(tmp_path: Path):
    stderr = StringIO()
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr)

    code = main(['run', '--handoff', '--session', 'agent-1', 'go'], host=None, io=io)

    assert code == 2
    assert '期望: --handoff 用于一次新的 run' in stderr.getvalue()
    assert '去掉 --session' in stderr.getvalue()


def test_run_failed_without_changes_does_not_mention_uncommitted_work(tmp_path: Path):
    root = _workspace(tmp_path)
    run = _run(
        tmp_path,
        ScriptedLLM([ScriptedTurn(raises=RuntimeError('boom'))]),
        config=_config(tmp_path),
        root=root,
    )

    assert run.code == 1
    assert run.matter['status'] == 'run_failed'
    assert '修好之后重跑。' in run.stdout
    assert '工作区里的改动还在' not in run.stdout


def test_run_failed_with_changes_mentions_uncommitted_work(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'Write', path='src/new.py', content='NEW = 1\n')]),
            ScriptedTurn(raises=RuntimeError('boom after write')),
        ]
    )
    run = _run(tmp_path, llm, config=_config(tmp_path), root=root)

    assert run.code == 1
    assert run.matter['status'] == 'run_failed'
    assert '工作区里的改动还在，没有提交。' in run.stdout


def test_the_handoff_names_the_runs_trace_and_the_trace_hides_the_hosts_secrets(tmp_path: Path):
    root = _workspace(tmp_path)
    answer = f'done with {_SECRET}\ncommit_title: add new'

    run = _run(
        tmp_path, _reviewed(answer=answer), config=_config(tmp_path, verify=['true']), root=root
    )

    (path,) = run.files
    document = path.read_text(encoding='utf-8')
    [directory] = (root / '.syndica' / 'traces').iterdir()
    texts = [file.read_text(encoding='utf-8') for file in directory.rglob('*') if file.is_file()]
    assert f'\n## trace\n\n`.syndica/traces/{directory.name}`\n' in document
    assert f'trace：.syndica/traces/{directory.name}\n' in run.stderr
    assert 'trace' not in run.matter
    assert texts and not any(_SECRET in text for text in texts)
    assert any('done with [REDACTED:secret.env_value]' in text for text in texts)


class _LockingTraces(ScriptedLLM):
    """第一次被调用时把这次 run 的 trace 目录改成只读：Agent 的 Bash 能做到的事。"""

    def __init__(self, turns: list[ScriptedTurn], root: Path) -> None:
        super().__init__(turns)
        self._root = root
        self.locked: list[Path] = []

    async def create(self, messages: list[Any], **kwargs: Any) -> Any:
        if not self.locked:
            [directory] = (self._root / '.syndica' / 'traces').iterdir()
            directory.chmod(0o500)
            self.locked.append(directory)
        return await super().create(messages, **kwargs)


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_a_handoff_whose_trace_lost_records_says_the_trace_is_incomplete(tmp_path: Path):
    root = _workspace(tmp_path)
    llm = _LockingTraces([ScriptedTurn(content='nothing to change')], root)
    try:
        run = _run(tmp_path, llm, config=_config(tmp_path, verify=['true']), root=root)
    finally:
        for directory in llm.locked:
            directory.chmod(0o700)

    (path,) = run.files
    document = path.read_text(encoding='utf-8')
    [directory] = llm.locked
    assert f'`.syndica/traces/{directory.name}`\n\n不完整：有记录没写进去' in document
    assert len([line for line in run.stderr.splitlines() if line.startswith('期望: trace ')]) == 1
