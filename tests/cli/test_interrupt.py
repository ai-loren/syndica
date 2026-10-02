"""Ctrl-C（M3 D15 末条）：在子进程里跑 CLI、发真的 SIGINT。

在测试进程里发 SIGINT，漏出来的 KeyboardInterrupt 会停掉整个 pytest 会话而不是让一条用例变红，
所以每条用例都起一个子进程：脚本化的 LLM、真的 `entrypoint`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any, TextIO, cast

import pytest

import syndica.__main__ as entry
from syndica.cli.host import (
    RuntimeCliHost,
    _CtrlC,  # pyright: ignore[reportPrivateUsage]
    _interruptible,  # pyright: ignore[reportPrivateUsage]
    _Lines,  # pyright: ignore[reportPrivateUsage]
)
from syndica.cli.main import CliIO
from syndica.kernel import FileTraceStore, ScriptedLLM, SpanStatus, TraceHeader

_DRIVER = """
import json
import sys
from pathlib import Path

import syndica.__main__ as entry
from syndica.kernel import ScriptedLLM, ScriptedTurn, ToolCall

spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
llm = ScriptedLLM(
    [
        ScriptedTurn(
            content=turn.get('content'),
            tool_calls=[
                ToolCall(id=call['id'], name=call['name'], arguments=json.dumps(call['arguments']))
                for call in turn.get('tool_calls', [])
            ],
        )
        for turn in spec['turns']
    ]
)
entry._configured_model = lambda name: llm
if spec.get('slow_finish'):
    import asyncio

    from syndica.cli import recording

    original = recording.Recording.finish

    async def finish(self, status):
        Path(spec['slow_finish']).write_text('finishing', encoding='utf-8')
        await asyncio.sleep(1.5)
        return await original(self, status)

    recording.Recording.finish = finish
sys.argv = ['syndica', *spec['argv']]
entry.entrypoint()
"""
# 子进程里的事都有上限：机器忙时起一个 Python、建一个 Runtime 就要好几秒。
_WITHIN = 30.0


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    config = tmp_path / 'global.gitconfig'
    config.write_text(
        '[user]\n\tname = Syndica Test\n\temail = test@example.com\n'
        '[init]\n\tdefaultBranch = main\n',
        encoding='utf-8',
    )
    environ = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return {
        **environ,
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': str(config),
        'GIT_CONFIG_NOSYSTEM': '1',
    }


def _git(root: Path, tmp_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ['git', *args], cwd=root, env=_env(tmp_path), check=True, capture_output=True, text=True
    )
    return completed.stdout


def _workspace(tmp_path: Path, verify: list[str] | None = None) -> Path:
    root = tmp_path / 'ws'
    (root / 'src').mkdir(parents=True)
    (root / 'src' / 'keep.py').write_text('KEEP = 1\n', encoding='utf-8')
    _git(tmp_path, tmp_path, 'init', '-q', '-b', 'main', str(root))
    _git(root, tmp_path, 'add', '.')
    _git(root, tmp_path, 'commit', '-q', '-m', 'init')
    (tmp_path / 'lead.md').write_text('You lead.\n', encoding='utf-8')
    (tmp_path / 'reviewer.md').write_text('You review.\n', encoding='utf-8')
    lines = [
        'version: 1',
        'agents:',
        '  - name: Reviewer',
        '    description: reviews',
        f'    system_prompt: {tmp_path / "reviewer.md"}',
        '    tools: [Read, Bash]',
        '  - name: Lead',
        '    description: leads',
        f'    system_prompt: {tmp_path / "lead.md"}',
        '    tools: [Write, Bash]',
        '    subagents: [Reviewer]',
    ]
    if verify is not None:
        lines.append(f'workspace: {json.dumps({"verify": verify})}')
    (tmp_path / 'syndica.yaml').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return root


def _call(call_id: str, name: str, **arguments: object) -> dict[str, Any]:
    return {'tool_calls': [{'id': call_id, 'name': name, 'arguments': arguments}]}


def _waiting(name: str) -> str:
    """一条记下自己 pid、立起标记、然后一直等着的命令：`exec` 之后 pid 就是那个 sleep。"""
    return f'echo $$ > {name}.pid && touch {name}.ready && exec sleep 60'


class _Child:
    def __init__(
        self,
        tmp_path: Path,
        root: Path,
        argv: list[str],
        turns: list[dict[str, Any]],
        *,
        slow_finish: Path | None = None,
    ):
        driver = tmp_path / 'driver.py'
        driver.write_text(_DRIVER, encoding='utf-8')
        spec = tmp_path / 'spec.json'
        values: dict[str, Any] = {'argv': argv, 'turns': turns}
        if slow_finish is not None:
            values['slow_finish'] = str(slow_finish)
        spec.write_text(json.dumps(values), encoding='utf-8')
        self.root = root
        self.process = subprocess.Popen(
            [sys.executable, str(driver), str(spec)],
            cwd=root,
            env=_env(tmp_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._out: list[bytes] = []
        self._err: list[bytes] = []
        self._readers = [
            threading.Thread(target=self._drain, args=(self.process.stdout, self._out)),
            threading.Thread(target=self._drain, args=(self.process.stderr, self._err)),
        ]
        for reader in self._readers:
            reader.start()

    @staticmethod
    def _drain(stream: Any, into: list[bytes]) -> None:
        for chunk in iter(lambda: stream.read1(4096), b''):
            into.append(chunk)

    @property
    def stdout(self) -> str:
        return b''.join(self._out).decode('utf-8', 'replace')

    @property
    def stderr(self) -> str:
        return b''.join(self._err).decode('utf-8', 'replace')

    def wait_for(self, ready: Any, what: str) -> None:
        deadline = time.monotonic() + _WITHIN
        while not ready():
            if self.process.poll() is not None:
                pytest.fail(f'the CLI exited before {what}:\n{self.stdout}\n{self.stderr}')
            if time.monotonic() > deadline:
                self.process.kill()
                pytest.fail(f'{what} did not happen within {_WITHIN:g}s:\n{self.stderr}')
            time.sleep(0.05)

    def interrupt(self, presses: int = 1, gap: float = 0.0) -> int:
        for press in range(presses):
            if press:
                time.sleep(gap)
            self.process.send_signal(signal.SIGINT)
        return self.finish('of Ctrl-C')

    def finish(self, after: str = 'on its own') -> int:
        try:
            code = self.process.wait(timeout=_WITHIN)
        except subprocess.TimeoutExpired:
            self.process.kill()
            pytest.fail(f'the CLI did not exit within {_WITHIN:g}s {after}:\n{self.stderr}')
        assert self.process.stdin is not None
        self.process.stdin.close()
        for reader in self._readers:
            reader.join(_WITHIN)
        return code

    def handoffs(self) -> list[Path]:
        directory = self.root / '.syndica' / 'handoffs'
        return sorted(directory.iterdir()) if directory.exists() else []


def _matter(document: str) -> dict[str, str]:
    head = document.split('---\n')[1]
    return dict(line.split(': ', 1) for line in head.splitlines())


def _assert_gone(pid: int) -> None:
    deadline = time.monotonic() + _WITHIN
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    pytest.fail(f'process {pid} outlived the interrupted CLI')


def _traces(root: Path) -> list[TraceHeader]:
    return asyncio.run(FileTraceStore(root / '.syndica' / 'traces').traces())


def _handoff_argv(tmp_path: Path) -> list[str]:
    return ['--config', str(tmp_path / 'syndica.yaml'), 'run', '--handoff', '--agent', 'Lead', 'go']


def test_ctrl_c_during_the_run_writes_a_failed_handoff_naming_its_cancelled_trace(
    tmp_path: Path,
):
    root = _workspace(tmp_path, verify=['true'])
    child = _Child(
        tmp_path, root, _handoff_argv(tmp_path), [_call('b', 'Bash', command=_waiting('run'))]
    )

    child.wait_for((root / 'run.ready').exists, 'the agent command started')
    pid = int((root / 'run.pid').read_text(encoding='utf-8'))
    code = child.interrupt()

    (path,) = child.handoffs()
    matter = _matter(child.stdout)
    [trace] = _traces(root)
    [directory] = (root / '.syndica' / 'traces').iterdir()
    assert trace.status is SpanStatus.CANCELLED
    assert f'## trace\n\n`.syndica/traces/{directory.name}`\n' in child.stdout
    assert code == 130
    assert 'Traceback' not in child.stderr
    assert (matter['status'], matter['host_verify'], matter['git'], matter['exit_code']) == (
        'run_failed',
        'skipped',
        'not_offered',
        '130',
    )
    assert '被中断（Ctrl-C）：run 没有跑完' in child.stdout
    assert '被中断（Ctrl-C），不做 git 收尾' in child.stdout
    assert path.read_text(encoding='utf-8') == child.stdout.rstrip('\n') + '\n'
    _assert_gone(pid)


def test_ctrl_c_during_host_verification_writes_a_failed_handoff_and_exits_130(tmp_path: Path):
    root = _workspace(tmp_path, verify=[_waiting('verify')])
    turns = [_call('w', 'Write', path='src/new.py', content='NEW = 1\n'), {'content': 'done'}]
    child = _Child(tmp_path, root, _handoff_argv(tmp_path), turns)

    child.wait_for((root / 'verify.ready').exists, 'the verify command started')
    pid = int((root / 'verify.pid').read_text(encoding='utf-8'))
    code = child.interrupt()

    matter = _matter(child.stdout)
    assert code == 130
    assert 'Traceback' not in child.stderr
    assert (matter['status'], matter['host_verify'], matter['exit_code']) == (
        'run_failed',
        'skipped',
        '130',
    )
    assert '被中断（Ctrl-C）：宿主复核没有跑完。' in child.stdout
    _assert_gone(pid)


def test_ctrl_c_at_the_git_question_leaves_the_file_pending_and_exits_130(tmp_path: Path):
    root = _workspace(tmp_path, verify=['true'])
    head = _git(root, tmp_path, 'rev-parse', 'HEAD')
    turns = [
        _call('w', 'Write', path='src/new.py', content='NEW = 1\n'),
        _call('d', 'Delegate', agent='Reviewer', query='review'),
        {'content': 'looks fine\nverdict: pass'},
        {'content': 'done\ncommit_title: add new'},
    ]
    child = _Child(tmp_path, root, _handoff_argv(tmp_path), turns)

    child.wait_for(lambda: '选哪个' in child.stderr, 'the git question was asked')
    code = child.interrupt()

    (path,) = child.handoffs()
    on_disk = path.read_text(encoding='utf-8')
    assert code == 130
    assert 'Traceback' not in child.stderr
    assert 'git: pending\n' in on_disk and 'exit_code: null\n' in on_disk
    assert _matter(child.stdout)['exit_code'] == '130'
    assert '被中断（Ctrl-C）：git 收尾停在提问或提交的中途' in child.stdout
    assert '宿主看到的：在提问时被打断：没有提交，改动还在工作区里。' in child.stdout
    assert f'git 收尾被中断（Ctrl-C）：{path} 停在 git: pending。在提问时被打断' in child.stderr
    assert _git(root, tmp_path, 'rev-parse', 'HEAD') == head


def _reviewed_after_an_expired_question() -> list[dict[str, Any]]:
    return [
        _call('q', 'AskUser', question='which colour?', timeout=1),
        _call('w', 'Write', path='src/new.py', content='NEW = 1\n'),
        _call('d', 'Delegate', agent='Reviewer', query='review'),
        {'content': 'looks fine\nverdict: pass'},
        {'content': 'done\ncommit_title: add new'},
    ]


def test_after_a_question_expired_the_git_question_takes_the_first_answer(tmp_path: Path):
    root = _workspace(tmp_path, verify=['true'])
    child = _Child(tmp_path, root, _handoff_argv(tmp_path), _reviewed_after_an_expired_question())

    child.wait_for(lambda: '选哪个' in child.stderr, 'the git question was asked')
    assert child.process.stdin is not None
    child.process.stdin.write(b'3\n')
    child.process.stdin.flush()
    code = child.finish('after one answer')

    assert code == 0
    assert _matter(child.stdout)['git'] == 'declined'


def test_after_a_question_expired_ctrl_c_still_stops_the_git_question(tmp_path: Path):
    root = _workspace(tmp_path, verify=['true'])
    head = _git(root, tmp_path, 'rev-parse', 'HEAD')
    child = _Child(tmp_path, root, _handoff_argv(tmp_path), _reviewed_after_an_expired_question())

    child.wait_for(lambda: '选哪个' in child.stderr, 'the git question was asked')
    code = child.interrupt()

    assert code == 130
    assert _matter(child.stdout)['exit_code'] == '130'
    assert _git(root, tmp_path, 'rev-parse', 'HEAD') == head


def test_ctrl_c_mashed_at_the_git_question_still_prints_the_handoff(tmp_path: Path):
    root = _workspace(tmp_path, verify=['true'])
    turns = [
        _call('w', 'Write', path='src/new.py', content='NEW = 1\n'),
        _call('d', 'Delegate', agent='Reviewer', query='review'),
        {'content': 'looks fine\nverdict: pass'},
        {'content': 'done\ncommit_title: add new'},
    ]
    child = _Child(tmp_path, root, _handoff_argv(tmp_path), turns)

    child.wait_for(lambda: '选哪个' in child.stderr, 'the git question was asked')
    code = child.interrupt(presses=3, gap=0.001)

    (path,) = child.handoffs()
    assert code == 130
    assert _matter(child.stdout)['exit_code'] == '130'
    assert '宿主看到的：在提问时被打断' in child.stdout
    assert '命令可能没有做完' not in child.stderr
    assert 'git: pending\n' in path.read_text(encoding='utf-8')


def test_a_double_ctrl_c_during_the_run_still_writes_the_handoff(tmp_path: Path):
    root = _workspace(tmp_path, verify=['true'])
    child = _Child(
        tmp_path, root, _handoff_argv(tmp_path), [_call('b', 'Bash', command=_waiting('run'))]
    )

    child.wait_for((root / 'run.ready').exists, 'the agent command started')
    code = child.interrupt(presses=2, gap=0.01)

    (path,) = child.handoffs()
    assert code == 130
    assert 'Traceback' not in child.stderr
    assert _matter(child.stdout)['status'] == 'run_failed'
    assert path.read_text(encoding='utf-8') == child.stdout.rstrip('\n') + '\n'
    assert '再按一次强制退出' in child.stderr


def test_ctrl_c_as_the_handoff_is_written_never_reaches_git(tmp_path: Path):
    root = _workspace(tmp_path, verify=['true'])
    head = _git(root, tmp_path, 'rev-parse', 'HEAD')
    turns = [
        _call('w', 'Write', path='src/new.py', content='NEW = 1\n'),
        _call('d', 'Delegate', agent='Reviewer', query='review'),
        {'content': 'looks fine\nverdict: pass'},
        {'content': 'done\ncommit_title: add new'},
    ]
    child = _Child(tmp_path, root, _handoff_argv(tmp_path), turns)

    child.wait_for(lambda: '交接文件：' in child.stderr, 'the handoff was written')
    code = child.interrupt()

    assert code == 130
    assert 'Traceback' not in child.stderr
    assert _matter(child.stdout)['exit_code'] == '130'
    assert _git(root, tmp_path, 'rev-parse', 'HEAD') == head
    assert 'src/new.py' not in _git(root, tmp_path, 'diff', '--cached', '--name-only')


def test_a_run_whose_question_expired_does_not_wait_for_an_answer_afterwards(tmp_path: Path):
    root = _workspace(tmp_path)
    argv = ['--config', str(tmp_path / 'syndica.yaml'), 'run', '--agent', 'Lead', 'go']
    turns = [_call('q', 'AskUser', question='which colour?', timeout=1), {'content': 'done'}]
    child = _Child(tmp_path, root, argv, turns)

    code = child.finish()

    assert code == 0
    assert 'which colour?' in child.stdout and 'done' in child.stdout


def test_ctrl_c_in_an_ordinary_run_exits_130_and_ends_its_trace_cancelled(tmp_path: Path):
    root = _workspace(tmp_path)
    argv = ['--config', str(tmp_path / 'syndica.yaml'), 'run', '--agent', 'Lead', 'go']
    child = _Child(tmp_path, root, argv, [_call('b', 'Bash', command=_waiting('plain'))])

    child.wait_for((root / 'plain.ready').exists, 'the agent command started')
    pid = int((root / 'plain.pid').read_text(encoding='utf-8'))
    code = child.interrupt()

    [trace] = _traces(root)
    assert trace.status is SpanStatus.CANCELLED
    assert 'trace：.syndica/traces/' in child.stderr
    assert code == 130
    assert 'Traceback' not in child.stderr
    assert '被中断（Ctrl-C）：命令可能没有做完。' in child.stderr
    assert '没有的话重跑这条命令' in child.stderr
    _assert_gone(pid)


def test_ctrl_c_while_a_finished_run_finishes_its_trace_keeps_the_answer_and_the_trace(
    tmp_path: Path,
):
    root = _workspace(tmp_path)
    argv = ['--config', str(tmp_path / 'syndica.yaml'), 'run', '--agent', 'Lead', 'go']
    finishing = root / 'finishing'
    child = _Child(tmp_path, root, argv, [{'content': 'the whole answer'}], slow_finish=finishing)

    child.wait_for(finishing.exists, 'the trace started to finish')
    code = child.interrupt()

    [trace] = _traces(root)
    assert code == 0, child.stderr
    assert child.stdout.rstrip().endswith('the whole answer')
    assert '收尾 trace 时按了 Ctrl-C：run 已经做完，结论照常给出。' in child.stderr
    assert 'trace：.syndica/traces/' in child.stderr
    assert trace.status is SpanStatus.OK
    assert 'Traceback' not in child.stderr


def test_ctrl_c_while_a_finished_handoff_run_finishes_its_trace_keeps_answer_and_trace(
    tmp_path: Path,
):
    root = _workspace(tmp_path)
    finishing = root / 'finishing'
    child = _Child(
        tmp_path,
        root,
        _handoff_argv(tmp_path),
        [{'content': 'the whole answer'}],
        slow_finish=finishing,
    )

    child.wait_for(finishing.exists, 'the trace started to finish')
    code = child.interrupt()

    [handoff] = child.handoffs()
    document = handoff.read_text(encoding='utf-8')
    [trace] = _traces(root)
    assert code == 130, child.stderr
    assert 'the whole answer' in document
    assert '按键落在之后，复核与 git 收尾没有做' in document
    assert '## trace' in document and trace.trace_id[:8] in document
    assert trace.status is SpanStatus.OK


def test_ctrl_c_while_a_chat_agent_asks_the_human_ends_the_session_and_its_trace(tmp_path: Path):
    root = _workspace(tmp_path)
    config = tmp_path / 'syndica.yaml'
    config.write_text(
        config.read_text(encoding='utf-8').replace('name: Lead', 'name: Orchestrator'),
        encoding='utf-8',
    )
    child = _Child(
        tmp_path,
        root,
        ['--config', str(config), 'chat'],
        [_call('q', 'AskUser', question='which shape?')],
    )

    child.wait_for(lambda: ']> ' in child.stdout, 'the chat prompt appeared')
    assert child.process.stdin is not None
    child.process.stdin.write(b'hello\n')
    child.process.stdin.flush()
    child.wait_for(lambda: 'which shape?' in child.stdout, 'the question was asked')
    code = child.interrupt()

    [trace] = _traces(root)
    assert code == 130
    assert 'Traceback' not in child.stderr
    assert trace.status is SpanStatus.CANCELLED


def test_ctrl_c_while_the_agent_asks_the_human_does_not_wait_for_an_answer(tmp_path: Path):
    root = _workspace(tmp_path)
    argv = ['--config', str(tmp_path / 'syndica.yaml'), 'run', '--agent', 'Lead', 'go']
    child = _Child(tmp_path, root, argv, [_call('q', 'AskUser', question='which colour?')])

    child.wait_for(lambda: 'which colour?' in child.stdout, 'the question was asked')
    code = child.interrupt()

    assert code == 130
    assert 'Traceback' not in child.stderr


# ---- 进程内：不发信号 ----


def _press(ctrl_c: _CtrlC) -> None:
    ctrl_c(signal.SIGINT, None)


def test_a_press_while_work_runs_cancels_the_work_not_the_caller():
    warnings: list[str] = []
    ctrl_c = _CtrlC(warnings.append)

    async def work() -> str:
        await asyncio.sleep(_WITHIN)
        return 'finished'

    async def scenario() -> tuple[str, int]:
        waiting = asyncio.ensure_future(_interruptible(ctrl_c, work()))
        await asyncio.sleep(0)
        _press(ctrl_c)
        try:
            await waiting
        except asyncio.CancelledError:
            outcome = 'cancelled'
        else:
            outcome = 'finished'
        task = asyncio.current_task()
        assert task is not None
        return outcome, task.cancelling()

    assert asyncio.run(scenario()) == ('cancelled', 0)
    assert ctrl_c.presses == 1 and len(warnings) == 1


def test_work_started_after_a_press_is_cancelled_before_it_runs():
    ctrl_c = _CtrlC(lambda text: None)
    ran: list[bool] = []
    _press(ctrl_c)

    async def work() -> None:
        ran.append(True)

    async def scenario() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await _interruptible(ctrl_c, work())

    asyncio.run(scenario())
    assert ran == []


def test_presses_between_phases_are_only_recorded_until_the_third_forces_an_exit():
    warnings: list[str] = []
    ctrl_c = _CtrlC(warnings.append)

    _press(ctrl_c)
    _press(ctrl_c)

    assert ctrl_c.pressed and len(warnings) == 2
    assert '再按两次强制退出' in warnings[0] and '再按一次强制退出' in warnings[1]
    with pytest.raises(KeyboardInterrupt):
        _press(ctrl_c)


class _PressedAsTheGitStepEnds(_CtrlC):
    """git 收尾这一段放下 `_raising` 的那一刻再按一次：两句赋值之间也收得到信号。"""

    armed = False

    def __init__(self, warn: Callable[[str], None]) -> None:
        super().__init__(warn)
        self.late: list[BaseException | None] = []

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name == '_raising' and value is False and self.armed:
            self.armed = False
            try:
                _press(self)
            except KeyboardInterrupt as error:
                self.late.append(error)
            else:
                self.late.append(None)


def test_a_press_landing_just_as_the_git_step_ends_finds_the_outcome_settled():
    ctrl_c = _PressedAsTheGitStepEnds(lambda text: None)

    with pytest.raises(KeyboardInterrupt), ctrl_c.raising():
        try:
            _press(ctrl_c)
        finally:
            _press(ctrl_c)
            ctrl_c.armed = True

    assert ctrl_c.presses == 3
    assert ctrl_c.late == [None]


def test_once_settled_the_handler_outlives_the_run_until_it_is_released():
    ctrl_c = _CtrlC(lambda text: None)
    before = signal.getsignal(signal.SIGINT)

    async def run() -> None:
        with ctrl_c.installed():
            ctrl_c.settle()

    asyncio.run(run())
    kept = signal.getsignal(signal.SIGINT)
    _press(ctrl_c)
    ctrl_c.release(before)

    assert kept is ctrl_c
    assert signal.getsignal(signal.SIGINT) is before


def test_release_leaves_alone_a_handler_someone_else_installed_since():
    ctrl_c = _CtrlC(lambda text: None)
    before = signal.getsignal(signal.SIGINT)

    async def run() -> None:
        with ctrl_c.installed():
            ctrl_c.settle()

    def theirs(signum: int, frame: object) -> None:
        del signum, frame

    asyncio.run(run())
    signal.signal(signal.SIGINT, theirs)
    try:
        ctrl_c.release(before)
        assert signal.getsignal(signal.SIGINT) is theirs
    finally:
        signal.signal(signal.SIGINT, before)


def test_an_unsettled_run_puts_the_previous_handler_back_at_once():
    ctrl_c = _CtrlC(lambda text: None)
    before = signal.getsignal(signal.SIGINT)

    async def run() -> None:
        with ctrl_c.installed():
            pass

    asyncio.run(run())

    assert signal.getsignal(signal.SIGINT) is before


def test_in_the_git_step_only_the_first_press_raises_and_the_rest_are_told_once():
    warnings: list[str] = []
    ctrl_c = _CtrlC(warnings.append)
    started: list[bool] = []

    with pytest.raises(KeyboardInterrupt), ctrl_c.raising() as began:
        started.append(began)
        try:
            _press(ctrl_c)
        finally:
            for _ in range(3):
                _press(ctrl_c)

    assert started == [True]
    assert ctrl_c.presses == 4
    assert warnings == ['git 收尾正在停下（停钩子、撤回为提交暂存的新文件），都有时限，等它做完。']


def test_a_press_just_before_the_git_step_keeps_it_from_starting():
    ctrl_c = _CtrlC(lambda text: None)
    _press(ctrl_c)

    with ctrl_c.raising() as started:
        _press(ctrl_c)

    assert started is False
    assert ctrl_c.presses == 2


def test_once_the_outcome_is_settled_no_press_changes_anything_or_forces_an_exit():
    warnings: list[str] = []
    ctrl_c = _CtrlC(warnings.append)
    _press(ctrl_c)
    _press(ctrl_c)
    ctrl_c.settle()

    for _ in range(5):
        _press(ctrl_c)

    assert ctrl_c.presses == 7
    assert len(warnings) == 2


def test_after_the_git_step_no_press_forces_an_exit():
    ctrl_c = _CtrlC(lambda text: None)
    with pytest.raises(KeyboardInterrupt), ctrl_c.raising():
        _press(ctrl_c)

    for _ in range(5):
        _press(ctrl_c)

    assert ctrl_c.presses == 6


def test_a_warning_that_cannot_be_written_does_not_stop_the_press():
    def reentrant(text: str) -> None:
        raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>")

    ctrl_c = _CtrlC(reentrant)

    async def work() -> None:
        await asyncio.sleep(_WITHIN)

    async def scenario() -> bool:
        waiting = asyncio.ensure_future(_interruptible(ctrl_c, work()))
        await asyncio.sleep(0)
        _press(ctrl_c)
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        return waiting.cancelled()

    assert asyncio.run(scenario())


def test_the_handler_is_installed_for_the_block_and_the_previous_one_comes_back():
    def mine(signum: int, frame: object) -> None:
        return None

    ctrl_c = _CtrlC(lambda text: None)

    async def scenario() -> object:
        with ctrl_c.installed():
            return signal.getsignal(signal.SIGINT)

    previous = signal.signal(signal.SIGINT, mine)
    try:
        inside = asyncio.run(scenario())
        after = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert inside is ctrl_c
    assert after is mine


def test_an_inherited_ignore_of_ctrl_c_is_kept():
    ctrl_c = _CtrlC(lambda text: None)

    async def scenario() -> object:
        with ctrl_c.installed():
            return signal.getsignal(signal.SIGINT)

    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        inside = asyncio.run(scenario())
    finally:
        signal.signal(signal.SIGINT, previous)

    assert inside is signal.SIG_IGN


def test_outside_the_main_thread_nothing_is_installed():
    ctrl_c = _CtrlC(lambda text: None)
    seen: list[object] = []

    def run() -> None:
        async def scenario() -> None:
            with ctrl_c.installed():
                seen.append(signal.getsignal(signal.SIGINT))

        asyncio.run(scenario())

    before = signal.getsignal(signal.SIGINT)
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(_WITHIN)

    assert seen == [before]


def _readers() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name == 'syndica-stdin'}


def _abandon_a_wait(lines: _Lines) -> None:
    """等一行、还没读到就放弃（Ctrl-C、问题过期）：那次 readline 还在跑。"""

    async def scenario() -> None:
        waiting = asyncio.ensure_future(lines.next_line())
        await asyncio.sleep(0.05)
        waiting.cancel()

    asyncio.run(scenario())


def test_a_line_is_handed_over_as_soon_as_it_is_read():
    read_end, write_end = os.pipe()
    stream = os.fdopen(read_end, 'r')
    lines = _Lines(stream)

    async def scenario() -> tuple[str, float]:
        started = time.monotonic()
        waiting = asyncio.ensure_future(lines.next_line())
        await asyncio.sleep(0.05)
        os.write(write_end, b'yes\n')
        line = await asyncio.wait_for(waiting, _WITHIN)
        return line, time.monotonic() - started

    try:
        line, elapsed = asyncio.run(scenario())
    finally:
        os.close(write_end)
        stream.close()
    assert line == 'yes\n'
    assert elapsed < 2


def test_an_error_reading_the_answer_reaches_the_reader():
    class Broken:
        def readline(self) -> str:
            raise OSError('stdin went away')

    async def scenario() -> str:
        return await asyncio.wait_for(_Lines(cast(TextIO, Broken())).next_line(), _WITHIN)

    with pytest.raises(OSError, match='stdin went away'):
        asyncio.run(scenario())


def _typed(write_end: int, *lines: tuple[float, bytes]) -> list[threading.Timer]:
    """在给定的秒数之后往管道里写一行；用完要 cancel。"""
    timers = [threading.Timer(delay, os.write, (write_end, line)) for delay, line in lines]
    for timer in timers:
        timer.start()
    return timers


def test_a_line_typed_after_a_wait_was_given_up_reaches_the_next_wait():
    read_end, write_end = os.pipe()
    stream = os.fdopen(read_end, 'r')
    lines = _Lines(stream)
    timers: list[threading.Timer] = []

    try:
        _abandon_a_wait(lines)
        timers = _typed(write_end, (0.05, b'2\n'), (5.0, b'rescue\n'))
        answer = lines.readline()
    finally:
        for timer in timers:
            timer.cancel()
        os.close(write_end)
        stream.close()
    assert answer == '2\n'


def test_a_line_that_arrived_before_a_wait_began_is_not_taken_as_its_answer():
    read_end, write_end = os.pipe()
    stream = os.fdopen(read_end, 'r')
    lines = _Lines(stream)
    before = _readers()
    timers: list[threading.Timer] = []

    try:
        _abandon_a_wait(lines)
        (reader,) = _readers() - before
        os.write(write_end, b'meant for the question that expired\n')
        reader.join(_WITHIN)
        timers = _typed(write_end, (0.05, b'3\n'), (5.0, b'rescue\n'))
        answer = lines.readline()
    finally:
        for timer in timers:
            timer.cancel()
        os.close(write_end)
        stream.close()
    assert answer == '3\n'


def test_waits_that_were_given_up_share_one_outstanding_read():
    read_end, write_end = os.pipe()
    stream = os.fdopen(read_end, 'r')
    lines = _Lines(stream)
    before = _readers()

    try:
        _abandon_a_wait(lines)
        _abandon_a_wait(lines)
        started = _readers() - before
    finally:
        os.close(write_end)
        stream.close()
    assert len(started) == 1


def test_a_signal_handler_interrupts_a_wait_behind_an_outstanding_read():
    class Pressed(Exception):
        pass

    def handler(signum: int, frame: object) -> None:
        raise Pressed

    read_end, write_end = os.pipe()
    stream = os.fdopen(read_end, 'r')
    lines = _Lines(stream)
    previous = signal.signal(signal.SIGUSR1, handler)
    press = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGUSR1))
    rescue = threading.Timer(5.0, os.write, (write_end, b'late\n'))
    try:
        _abandon_a_wait(lines)
        rescue.start()
        press.start()
        started = time.monotonic()
        with pytest.raises(Pressed):
            lines.readline()
        elapsed = time.monotonic() - started
    finally:
        press.cancel()
        rescue.cancel()
        signal.signal(signal.SIGUSR1, previous)
        os.close(write_end)
        stream.close()
    assert elapsed < 3


def test_a_line_that_arrives_after_the_loop_closed_raises_nothing_in_the_reader(
    monkeypatch: pytest.MonkeyPatch,
):
    read_end, write_end = os.pipe()
    stream = os.fdopen(read_end, 'r')
    lines = _Lines(stream)
    failures: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, 'excepthook', failures.append)
    before = _readers()

    try:
        _abandon_a_wait(lines)
        (reader,) = _readers() - before
        os.write(write_end, b'late\n')
        reader.join(_WITHIN)
    finally:
        os.close(write_end)
        stream.close()
    assert not reader.is_alive()
    assert failures == []


def _exit_code_of_entrypoint() -> object:
    # 漏出来的 KeyboardInterrupt 会停掉整个 pytest 会话，接住它，让回归只是这一条用例变红。
    # entrypoint 退出前忽略 SIGINT：换回测试进程原来的处理。
    previous = signal.getsignal(signal.SIGINT)
    try:
        entry.entrypoint()
    except SystemExit as exited:
        return exited.code
    except KeyboardInterrupt:
        pytest.fail('KeyboardInterrupt escaped entrypoint')
    finally:
        signal.signal(signal.SIGINT, previous)
    pytest.fail('entrypoint returned instead of exiting')


def test_once_the_host_is_closed_ctrl_c_is_ignored_while_the_interpreter_exits(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(entry, 'main', lambda **_: 0)
    monkeypatch.setattr(entry._LazyHost, 'close', lambda self, **_: None)  # pyright: ignore[reportPrivateUsage]
    previous = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(SystemExit) as exited:
            entry.entrypoint()
        at_exit = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert exited.value.code == 0
    assert at_exit is signal.SIG_IGN


def test_a_ctrl_c_while_the_host_closes_keeps_the_exit_code_already_reported(
    monkeypatch: pytest.MonkeyPatch,
):
    def interrupted(self: object, **_: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, 'main', lambda **_: 3)
    monkeypatch.setattr(entry._LazyHost, 'close', interrupted)  # pyright: ignore[reportPrivateUsage]

    assert _exit_code_of_entrypoint() == 3


@pytest.mark.parametrize(('outcome', 'interrupted'), [(0, False), (3, False), (130, True)])
def test_the_host_is_told_whether_the_command_ended_interrupted(
    monkeypatch: pytest.MonkeyPatch, outcome: int, interrupted: bool
):
    told: list[bool] = []

    def close(self: object, *, interrupted: bool = False) -> None:
        told.append(interrupted)

    monkeypatch.setattr(entry, 'main', lambda **_: outcome)
    monkeypatch.setattr(entry._LazyHost, 'close', close)  # pyright: ignore[reportPrivateUsage]

    assert _exit_code_of_entrypoint() == outcome
    assert told == [interrupted]


def test_a_ctrl_c_escaping_main_tells_the_host_it_was_interrupted(
    monkeypatch: pytest.MonkeyPatch,
):
    told: list[bool] = []

    def interrupted(**_: object) -> int:
        raise KeyboardInterrupt

    def close(self: object, *, interrupted: bool = False) -> None:
        told.append(interrupted)

    monkeypatch.setattr(entry, 'main', interrupted)
    monkeypatch.setattr(entry._LazyHost, 'close', close)  # pyright: ignore[reportPrivateUsage]

    assert _exit_code_of_entrypoint() == 130
    assert told == [True]


def test_a_second_ctrl_c_while_main_reports_the_first_still_exits_130(
    monkeypatch: pytest.MonkeyPatch,
):
    def interrupted(**_: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, 'main', interrupted)
    monkeypatch.setattr(entry._LazyHost, 'close', lambda self, **_: None)  # pyright: ignore[reportPrivateUsage]

    assert _exit_code_of_entrypoint() == 130


def _closing_host(tmp_path: Path) -> RuntimeCliHost:
    io = CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO())
    return RuntimeCliHost(
        io=io, model_factory=lambda _: ScriptedLLM([]), workspace_root=tmp_path, environ={}
    )


@pytest.mark.parametrize('surfaced_as', ['KeyboardInterrupt', 'RuntimeError'])
def test_a_ctrl_c_while_the_host_closes_its_loop_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surfaced_as: str
):
    host = _closing_host(tmp_path)
    runner = host._runner  # pyright: ignore[reportPrivateUsage]
    real_close = runner.close

    def interrupted() -> None:
        real_close()
        try:
            signal.raise_signal(signal.SIGINT)
        except KeyboardInterrupt:
            if surfaced_as == 'KeyboardInterrupt':
                raise
            raise RuntimeError('Event loop stopped before Future completed.') from None

    monkeypatch.setattr(runner, 'close', interrupted)

    host.close()

    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_loop_that_fails_to_close_without_a_press_still_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    host = _closing_host(tmp_path)
    runner = host._runner  # pyright: ignore[reportPrivateUsage]
    real_close = runner.close

    def broken() -> None:
        real_close()
        raise RuntimeError('a bug in closing')

    monkeypatch.setattr(runner, 'close', broken)

    with pytest.raises(RuntimeError, match='a bug in closing'):
        host.close()


def test_the_loop_is_closed_even_when_closing_the_sessions_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    host = _closing_host(tmp_path)

    async def interrupted(*, interrupted: bool = False) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(host, '_close_sessions', interrupted)

    with pytest.raises(KeyboardInterrupt):
        host.close()

    with pytest.raises(RuntimeError, match='closed'):
        host._runner.get_loop()  # pyright: ignore[reportPrivateUsage]


def test_a_press_while_the_sessions_close_stays_an_interruption_when_the_loop_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    host = _closing_host(tmp_path)
    runner = host._runner  # pyright: ignore[reportPrivateUsage]
    real_close = runner.close

    async def interrupted(*, interrupted: bool = False) -> None:
        raise KeyboardInterrupt

    def stopped() -> None:
        real_close()
        raise RuntimeError('Event loop stopped before Future completed.')

    monkeypatch.setattr(host, '_close_sessions', interrupted)
    monkeypatch.setattr(runner, 'close', stopped)

    with pytest.raises(KeyboardInterrupt):
        host.close()
