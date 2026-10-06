"""The CLI host composes real kernel components behind the lightweight router."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, InspectRequest, RunRequest, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    ConfigError,
    Context,
    ContextBlock,
    ContractViolation,
    LLMResponseError,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    SyndicaError,
    ToolCall,
)
from syndica.kernel.history_blocks import kind_of
from syndica.kernel.runtime import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]


def test_run_request_uses_the_real_runtime_with_an_injected_model(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='runtime completed')])
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO())
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(RunRequest(prompt='do the work', agent='Worker'))
    finally:
        host.close()

    assert output == 'runtime completed'
    assert llm.calls[0][1].role == 'user'
    assert llm.calls[0][1].content == 'do the work'


def test_run_request_answers_human_tools_through_the_runtime_mailbox(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-1',
                        name='AskUser',
                        arguments='{"question":"Your name?","timeout":30}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='choose-1',
                        name='AskUserQuestion',
                        arguments=(
                            '{"question":"Pick one","options":["alpha","beta"],"timeout":30}'
                        ),
                    )
                ]
            ),
            ScriptedTurn(content='answers received'),
        ]
    )
    stdout = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(
            stdin=StringIO('Loren\nbeta\n'),
            stdout=stdout,
            stderr=StringIO(),
        ),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(RunRequest(prompt='ask me', agent='Worker'))
    finally:
        host.close()

    assert output == 'answers received'
    assert 'Your name?' in stdout.getvalue()
    assert 'Pick one' in stdout.getvalue()
    assert 'alpha' in stdout.getvalue()
    assert 'beta' in stdout.getvalue()


def test_run_request_can_approve_one_dangerous_tool_call(
    tmp_path: Path,
) -> None:
    doomed = tmp_path / 'doomed.txt'
    doomed.write_text('remove me', encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='taint-1',
                        name='Bash',
                        arguments='{"command":"printf taint"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='dangerous-1',
                        name='Bash',
                        arguments='{"command":"rm -rf -- doomed.txt"}',
                    )
                ]
            ),
            ScriptedTurn(content='removed'),
        ]
    )
    stdout = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO('approve\n'), stdout=stdout, stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='remove it',
                agent='Worker',
                flags=('untrusted_high_risk=ask',),
            )
        )
    finally:
        host.close()

    assert output == 'removed'
    assert doomed.exists() is False
    assert 'dangerous-1' in stdout.getvalue()
    assert 'rm -rf -- doomed.txt' in stdout.getvalue()


def test_named_topology_runs_its_root_with_the_declared_model(tmp_path: Path) -> None:
    config = tmp_path / 'team.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Specialist, description: custom, model: specialist-model}
topologies:
  review:
    slots:
      - {name: root, agent: Specialist, is_root: true, cardinality: singleton, boot: eager}
""",
        encoding='utf-8',
    )
    requested_models: list[str] = []

    def model_factory(name: str) -> ScriptedLLM:
        requested_models.append(name)
        content = 'topology completed' if name == 'specialist-model' else 'wrong model'
        return ScriptedLLM([ScriptedTurn(content=content)])

    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=model_factory,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(prompt='review', config=str(config), topology='review')
        )
    finally:
        host.close()

    assert output == 'topology completed'
    assert requested_models == ['default', 'specialist-model']


@pytest.mark.parametrize(
    ('changes', 'needle'),
    [
        ({'session': 'old-session'}, 'sessions resume'),
        ({'max_cost': 1.0}, 'Decimal'),
        ({'flags': ('missing=1',)}, 'missing'),
        ({'flags': ('max_wait_seconds=nope',)}, 'int'),
        ({'flags': ('untrusted_high_risk=permit',)}, 'permit'),
        ({'flags': ('max_wait_seconds=1', 'max_wait_seconds=2')}, '重复'),
    ],
)
def test_unsupported_or_invalid_run_options_fail_before_model_creation(
    tmp_path: Path,
    changes: dict[str, object],
    needle: str,
) -> None:
    calls: list[str] = []
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda name: calls.append(name) or ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    request = RunRequest(prompt='go')
    request = RunRequest(
        **{
            **request.__dict__,
            **changes,
        }
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(request)
    finally:
        host.close()

    assert needle in str(excinfo.value)
    assert excinfo.value.expected
    assert excinfo.value.received
    assert excinfo.value.next_action
    assert calls == []


def test_inspect_reads_agents_topologies_and_flags_from_their_sources(
    tmp_path: Path,
) -> None:
    config = tmp_path / 'team.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Specialist, description: custom role, tools: [Read]}
topologies:
  review:
    slots:
      - {name: root, agent: Specialist, is_root: true}
""",
        encoding='utf-8',
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    try:
        agents = host.inspect(
            InspectRequest(resource='agents', action='list', name=None, config=str(config))
        )
        agent = host.inspect(
            InspectRequest(
                resource='agents',
                action='show',
                name='Specialist',
                config=str(config),
            )
        )
        topologies = host.inspect(
            InspectRequest(resource='topology', action='list', name=None, config=str(config))
        )
        topology = host.inspect(
            InspectRequest(
                resource='topology',
                action='show',
                name='review',
                config=str(config),
            )
        )
        flags = host.inspect(
            InspectRequest(resource='flags', action='list', name=None, config=str(config))
        )
        flag = host.inspect(
            InspectRequest(
                resource='flags',
                action='show',
                name='untrusted_high_risk',
                config=str(config),
            )
        )
    finally:
        host.close()

    assert agents.splitlines() == ['Planner', 'Explorer', 'Worker', 'Orchestrator', 'Specialist']
    assert json.loads(agent)['description'] == 'custom role'
    assert json.loads(agent)['tools'] == ['Read']
    assert topologies == 'review'
    assert json.loads(topology)['slots'][0]['agent'] == 'Specialist'
    assert flags.splitlines() == [
        'max_wait_seconds\tsafety\tint\t0',
        'untrusted_high_risk\tsafety\tstr\t"reject"',
    ]
    assert json.loads(flag) == {
        'key': 'untrusted_high_risk',
        'module': 'safety',
        'type': 'str',
        'default': 'reject',
        'effective': 'reject',
        'source': '缺省',
        'description': '高风险调用可溯源到不可信内容时的处置',
        'choices': ['reject', 'ask'],
        'minimum': None,
        'maximum': None,
    }


def test_doctor_validates_config_models_and_workspace(tmp_path: Path) -> None:
    requested_models: list[str] = []
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda name: requested_models.append(name) or ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    try:
        result = host.doctor('.syndica.yaml').output
    finally:
        host.close()

    assert requested_models == ['default']
    assert f'workspace: ok {tmp_path}' in result
    assert 'config: ok .syndica.yaml' in result
    assert 'models: ok default' in result


def test_chat_session_reuses_one_agent_and_lists_the_current_target(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(content='first answer'),
            ScriptedTurn(content='second answer'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        target = host.open_chat('.syndica.yaml')
        first = host.run_request(RunRequest(prompt='first', session=target))
        second = host.run_request(RunRequest(prompt='second', session=target))
        active = host.active_agents()
    finally:
        host.close()

    assert first == 'first answer'
    assert second == 'second answer'
    assert target in active
    assert 'Orchestrator' in active
    assert len(llm.calls) == 2
    assert [b.content for b in llm.calls[1] if b.role == 'user' and kind_of(b) is None] == [
        'first',
        'second',
    ]


def test_eof_aborts_a_pending_human_question(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-1',
                        name='AskUser',
                        arguments='{"question":"Need input","timeout":30}',
                    )
                ]
            )
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='ask', agent='Worker'))
    finally:
        host.close()

    assert 'aborted' in excinfo.value.received
    assert 'stdin' in excinfo.value.received
    assert excinfo.value.next_action


def _break_the_pump_once_the_agent_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """读人的答复的那一侧在 Agent 结束之后、Runtime 关掉之前出错。"""
    finished = asyncio.Event()
    original = RuntimeCliHost._wait_with_pump  # pyright: ignore[reportPrivateUsage]

    async def broken_pump(*args: object) -> None:
        await finished.wait()
        raise OSError('stdin went away')

    async def wait_then_let_the_pump_break(
        self: RuntimeCliHost, runtime: Any, agent_id: str, pump: asyncio.Task[None]
    ) -> Context:
        context = await original(self, runtime, agent_id, pump)
        finished.set()
        await asyncio.gather(pump, return_exceptions=True)
        return context

    monkeypatch.setattr(RuntimeCliHost, '_event_pump', broken_pump)
    monkeypatch.setattr(RuntimeCliHost, '_wait_with_pump', wait_then_let_the_pump_break)


def test_a_human_input_failure_after_the_agent_finished_still_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='finished first')])
    _break_the_pump_once_the_agent_is_done(monkeypatch)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(OSError, match='stdin went away'):
            host.run_request(RunRequest(prompt='work', agent='Worker'))
    finally:
        host.close()


def test_a_human_input_failure_while_the_agent_waits_is_reported_once(tmp_path: Path) -> None:
    class Broken(StringIO):
        def readline(self, size: int | None = -1) -> str:
            raise OSError('stdin went away')

    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-1',
                        name='AskUser',
                        arguments='{"question":"Need input","timeout":30}',
                    )
                ]
            )
        ]
    )
    stderr = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=Broken(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(OSError, match='stdin went away'):
            host.run_request(RunRequest(prompt='ask', agent='Worker'))
    finally:
        host.close()

    assert '读人的答复时也出了错' not in stderr.getvalue()


def test_a_human_input_failure_does_not_replace_the_runs_own_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='finished first')])
    _break_the_pump_once_the_agent_is_done(monkeypatch)

    async def failed(*args: object, **kwargs: object) -> str:
        raise RuntimeError('the run itself failed')

    monkeypatch.setattr(RuntimeCliHost, '_answer', failed)
    stderr = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(RuntimeError, match='the run itself failed'):
            host.run_request(RunRequest(prompt='work', agent='Worker'))
    finally:
        host.close()

    assert '读人的答复时也出了错：OSError: stdin went away' in stderr.getvalue()


def test_eof_rejects_confirmation_without_executing_the_tool(tmp_path: Path) -> None:
    doomed = tmp_path / 'doomed.txt'
    doomed.write_text('keep me', encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='taint-1',
                        name='Bash',
                        arguments='{"command":"printf taint"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='dangerous-1',
                        name='Bash',
                        arguments='{"command":"rm -rf -- doomed.txt"}',
                    )
                ]
            ),
            ScriptedTurn(content='rejected'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='remove it',
                agent='Worker',
                flags=('untrusted_high_risk=ask',),
            )
        )
    finally:
        host.close()

    assert output == 'rejected'
    assert doomed.read_text(encoding='utf-8') == 'keep me'


def test_max_turns_limits_the_selected_agent(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='partial',
                tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')],
            ),
            ScriptedTurn(content='must not run'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(RunRequest(prompt='one turn only', agent='Worker', max_turns=1))
    finally:
        host.close()

    assert output == 'partial'
    assert len(llm.calls) == 1


def test_agent_error_is_reported_as_a_three_part_cli_failure(tmp_path: Path) -> None:
    llm = ScriptedLLM([ScriptedTurn(raises=RuntimeError('injected failure'))])
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='fail', agent='Worker'))
    finally:
        host.close()

    assert 'completed' in excinfo.value.expected
    assert 'error' in excinfo.value.received
    assert 'injected failure' in excinfo.value.received
    assert 'Runtime 最近错误' in excinfo.value.received
    assert '先处理' in excinfo.value.next_action


def test_agent_error_does_not_print_runtime_traceback_to_cli(
    tmp_path: Path,
) -> None:
    logger_output = StringIO()
    handler = logging.StreamHandler(logger_output)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.ERROR)
    error = LLMResponseError(
        expected='OpenAI 兼容端点返回一条可解析的 assistant 消息',
        received=(
            'HTTP 请求失败：HTTP 429 Too Many Requests: '
            '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'
        ),
        next_action='检查 base_url、认证信息、模型名、请求参数和端点状态后重试',
    )
    llm = ScriptedLLM([ScriptedTurn(raises=error)])
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='fail', agent='Worker'))
    finally:
        host.close()
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert 'free-models-per-day' in excinfo.value.received
    assert 'Rate limit exceeded' in excinfo.value.received
    assert logger_output.getvalue() == ''


def test_a_root_agent_failing_on_a_syndica_error_shows_that_error_once(tmp_path: Path) -> None:
    error = LLMResponseError(
        expected='模型服务返回一条 assistant 消息',
        received='HTTP 429：免费额度用完了',
        next_action='换一个模型，或等额度恢复后重试',
    )
    llm = ScriptedLLM([ScriptedTurn(raises=error)])
    stderr = StringIO()
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE, io=io, model_factory=lambda _: llm, workspace_root=tmp_path
    )
    try:
        code = main(['run', '--agent', 'Worker', 'fail'], host=host, io=io)
    finally:
        host.close()

    text = stderr.getvalue()
    assert code == 1
    assert text.count('下一步') == 1
    assert '下一步: 换一个模型，或等额度恢复后重试' in text
    assert '实际: HTTP 429：免费额度用完了' in text
    assert re.search(r'^Agent Worker（[^）]+）以错误结束：$', text, flags=re.MULTILINE)
    assert 'Runtime 最近错误' not in text


def test_after_a_subagent_failed_the_roots_own_error_is_the_one_shown(tmp_path: Path) -> None:
    child_error = LLMResponseError(
        expected='子 Agent 的模型返回一条消息',
        received='子 Agent 的请求超时',
        next_action='子 Agent 的下一步',
    )
    root_error = LLMResponseError(
        expected='根 Agent 的模型返回一条消息',
        received='根 Agent 的请求被限流',
        next_action='根 Agent 的下一步',
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d',
                        name='Delegate',
                        arguments=json.dumps({'agent': 'Worker', 'query': 'go'}),
                    )
                ]
            ),
            ScriptedTurn(raises=child_error),
            ScriptedTurn(raises=root_error),
        ]
    )
    stderr = StringIO()
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE, io=io, model_factory=lambda _: llm, workspace_root=tmp_path
    )
    try:
        code = main(['run', 'fail twice'], host=host, io=io)
    finally:
        host.close()

    text = stderr.getvalue()
    final = text.split('以错误结束：', 1)[1]
    assert code == 1
    assert '下一步: 根 Agent 的下一步' in final
    assert '子 Agent 的下一步' not in final
    assert 'Runtime 错误 1/1' in text.split('以错误结束：', 1)[0]


def test_a_chat_agent_failing_on_a_syndica_error_is_named_with_its_own_error(
    tmp_path: Path,
) -> None:
    error = LLMResponseError(
        expected='模型服务返回一条 assistant 消息',
        received='HTTP 429：免费额度用完了',
        next_action='换一个模型，或等额度恢复后重试',
    )
    llm = ScriptedLLM([ScriptedTurn(raises=error)])
    stderr = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        target = host.open_chat('.syndica.yaml')
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='hello', session=target))
    finally:
        host.close()

    assert excinfo.value.next_action == '换一个模型，或等额度恢复后重试'
    assert getattr(excinfo.value, 'headline', None) == f'Agent Orchestrator（{target}）以错误结束：'
    assert '以错误结束' not in stderr.getvalue()


class _PerAgentLLM(ScriptedLLM):
    """按正在驱动的那个 Agent 分发（内核的 contextvar）：并发的几个 Agent 各走各的剧本。"""

    def __init__(
        self, behave: Callable[[str, int, list[ContextBlock]], Awaitable[ContextBlock]]
    ) -> None:
        super().__init__([])
        self._behave = behave
        self._counts: dict[str, int] = {}

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        agent = _DRIVING_AGENT.get()
        assert agent is not None
        turn = self._counts.get(agent, 0)
        self._counts[agent] = turn + 1
        return await self._behave(agent, turn, messages)


def _tool_turn(name: str, **arguments: object) -> ContextBlock:
    call = ToolCall(id=f'{name}-call', name=name, arguments=json.dumps(arguments))
    return ContextBlock(role='assistant', content='', tool_calls=[call])


_ROOT_ERROR = LLMResponseError(
    expected='根 Agent 的模型返回一条消息',
    received='根 Agent 的请求被限流',
    next_action='根 Agent 的下一步',
)
_CHILD_ERROR = LLMResponseError(
    expected='子 Agent 的模型返回一条消息',
    received='子 Agent 的请求超时',
    next_action='子 Agent 的下一步',
)


def _cli_run(tmp_path: Path, llm: ScriptedLLM, argv: list[str]) -> tuple[int, str]:
    stderr = StringIO()
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE, io=io, model_factory=lambda _: llm, workspace_root=tmp_path
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close()
    return code, stderr.getvalue()


def test_a_root_failure_followed_by_its_abandoned_input_note_is_still_its_own_error(
    tmp_path: Path,
) -> None:
    ids: dict[str, str] = {}

    async def behave(agent: str, turn: int, _: list[ContextBlock]) -> ContextBlock:
        ids.setdefault('root', agent)
        if agent == ids['root']:
            if turn == 0:
                return _tool_turn('Delegate', agent='Worker', query='go')
            raise _ROOT_ERROR
        if turn == 0:
            return _tool_turn(
                'SendMessage', agent_id=ids['root'], content='fyi', trigger='deferred'
            )
        return ContextBlock(role='assistant', content='child done')

    code, text = _cli_run(tmp_path, _PerAgentLLM(behave), ['run', 'fail'])

    earlier, final = text.split('以错误结束：', 1)
    assert code == 1
    assert [line for line in final.splitlines() if line.startswith('下一步')] == [
        '下一步: 根 Agent 的下一步'
    ]
    assert 'Runtime 错误 1/1' in earlier


def test_a_child_failing_right_after_the_root_does_not_take_the_roots_place(
    tmp_path: Path,
) -> None:
    ids: dict[str, str] = {}
    root_logged = asyncio.Event()

    class _OnRootRecord(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.exc_info is not None and record.exc_info[1] is _ROOT_ERROR:
                root_logged.set()

    async def behave(agent: str, turn: int, _: list[ContextBlock]) -> ContextBlock:
        ids.setdefault('root', agent)
        if agent == ids['root']:
            if turn == 0:
                return _tool_turn('Delegate', agent='Worker', query='go')
            if turn == 1:
                return _tool_turn('SendMessage', agent_id=ids['child'], content='again')
            raise _ROOT_ERROR
        ids.setdefault('child', agent)
        if turn == 0:
            return ContextBlock(role='assistant', content='child done')
        await root_logged.wait()
        raise _CHILD_ERROR

    handler = _OnRootRecord(logging.ERROR)
    logger = logging.getLogger('syndica.kernel.runtime')
    logger.addHandler(handler)
    try:
        code, text = _cli_run(tmp_path, _PerAgentLLM(behave), ['run', 'fail'])
    finally:
        logger.removeHandler(handler)

    final = text.split('以错误结束：', 1)[1]
    assert code == 1
    assert f'Agent Orchestrator（{ids["root"]}）以错误结束：' in text
    assert '下一步: 根 Agent 的下一步' in final
    assert '子 Agent' not in final


def test_a_root_crash_after_a_childs_syndica_error_stays_nested_with_the_roots_crash(
    tmp_path: Path,
) -> None:
    ids: dict[str, str] = {}

    async def behave(agent: str, turn: int, _: list[ContextBlock]) -> ContextBlock:
        ids.setdefault('root', agent)
        if agent == ids['root']:
            if turn == 0:
                return _tool_turn('Delegate', agent='Worker', query='go')
            raise RuntimeError('root crashed')
        raise _CHILD_ERROR

    code, text = _cli_run(tmp_path, _PerAgentLLM(behave), ['run', 'fail'])

    final = text.split('期望: CLI 请求对应的 Agent', 1)[1]
    assert code == 1
    assert '以错误结束：' not in text
    assert 'root crashed' in final
    assert '子 Agent 的下一步' not in final


def test_the_whole_underlying_error_is_shown_including_a_multiline_received(
    tmp_path: Path,
) -> None:
    error = LLMResponseError(
        expected='端点返回一条 assistant 消息',
        received='HTTP 429\n{"error":"free-models-per-day"}',
        next_action='换一个模型',
    )

    code, text = _cli_run(
        tmp_path, ScriptedLLM([ScriptedTurn(raises=error)]), ['run', '--agent', 'Worker', 'x']
    )

    assert code == 1
    assert '期望: 端点返回一条 assistant 消息' in text
    assert '{"error":"free-models-per-day"}' in text
    assert 'completed / max_turns / aborted' not in text


@pytest.mark.parametrize('kind', [SyndicaError, ConfigError, ContractViolation])
def test_every_kind_of_syndica_error_is_shown_de_nested(
    tmp_path: Path, kind: type[SyndicaError]
) -> None:
    error = kind(expected='某个前提', received='它不成立', next_action='修好前提再跑')

    code, text = _cli_run(
        tmp_path, ScriptedLLM([ScriptedTurn(raises=error)]), ['run', '--agent', 'Worker', 'x']
    )

    assert code == 1
    assert text.count('下一步') == 1
    assert '下一步: 修好前提再跑' in text


def test_the_headline_escapes_an_agent_name_from_the_config(tmp_path: Path) -> None:
    (tmp_path / 'p.md').write_text('You work.\n', encoding='utf-8')
    config = tmp_path / 'evil.yaml'
    config.write_text(
        'version: 1\nagents:\n  - name: "Evil\\e]0;pwned\\a"\n    description: d\n'
        '    system_prompt: p.md\n    tools: [Read]\n',
        encoding='utf-8',
    )
    error = LLMResponseError(expected='e', received='r', next_action='n')

    code, text = _cli_run(
        tmp_path,
        ScriptedLLM([ScriptedTurn(raises=error)]),
        ['--config', str(config), 'run', '--agent', 'Evil\x1b]0;pwned\x07', 'x'],
    )

    assert code == 1
    assert 'Agent Evil\\x1b]0;pwned\\x07（' in text
    assert '\x1b' not in text and '\x07' not in text


def test_a_chat_target_chosen_by_id_is_the_one_named(tmp_path: Path) -> None:
    ids: dict[str, str] = {}
    child_error = LLMResponseError(expected='子 期望', received='子 实际', next_action='子 收尾')

    async def behave(agent: str, turn: int, _: list[ContextBlock]) -> ContextBlock:
        ids.setdefault('root', agent)
        if agent == ids['root']:
            if turn == 0:
                return _tool_turn('Delegate', agent='Worker', query='go')
            return ContextBlock(role='assistant', content='root ok')
        ids.setdefault('child', agent)
        if turn == 0:
            return ContextBlock(role='assistant', content='child ok')
        raise child_error

    llm = _PerAgentLLM(behave)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        target = host.open_chat('.syndica.yaml')
        assert host.run_request(RunRequest(prompt='first', session=target)) == 'root ok'
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='again', session=ids['child']))
    finally:
        host.close()

    assert getattr(excinfo.value, 'headline', None) == f'Agent Worker（{ids["child"]}）以错误结束：'


def test_running_a_topology_without_a_root_fails_with_actionable_guidance(
    tmp_path: Path,
) -> None:
    config = tmp_path / 'rootless.yaml'
    config.write_text(
        """
version: 1
topologies:
  rootless:
    slots:
      - {name: worker, agent: Worker, cardinality: singleton}
""",
        encoding='utf-8',
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(RunRequest(prompt='go', config=str(config), topology='rootless'))
    finally:
        host.close()

    assert 'root' in excinfo.value.expected
    assert 'is_root' in excinfo.value.received
    assert 'is_root: true' in excinfo.value.next_action


def test_chat_can_switch_from_the_root_to_a_delegated_agent(tmp_path: Path) -> None:
    config = tmp_path / 'chat.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Worker, description: custom worker, model: worker-model}
""",
        encoding='utf-8',
    )
    root_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='delegate-1',
                        name='Delegate',
                        arguments='{"agent":"Worker","query":"first child task"}',
                    )
                ]
            ),
            ScriptedTurn(content='root done'),
        ]
    )
    worker_llm = ScriptedLLM(
        [
            ScriptedTurn(content='child first'),
            ScriptedTurn(content='child continued'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda name: worker_llm if name == 'worker-model' else root_llm,
        workspace_root=tmp_path,
    )
    try:
        root_id = host.open_chat(str(config))
        root_output = host.run_request(
            RunRequest(prompt='delegate', config=str(config), session=root_id)
        )
        assert root_output == 'root done'
        active = host.active_agents().splitlines()
        worker_id = next(line.split()[0] for line in active if ' Worker ' in line)
        continued = host.run_request(
            RunRequest(prompt='continue directly', config=str(config), session=worker_id)
        )
    finally:
        host.close()

    assert continued == 'child continued'
    assert [b.content for b in worker_llm.calls[1] if b.role == 'user' and kind_of(b) is None] == [
        'first child task',
        'continue directly',
    ]


@pytest.mark.parametrize(
    'changes',
    [
        {'max_turns': 1},
        {'flags': ('max_wait_seconds=1',)},
    ],
)
def test_chat_session_rejects_options_that_cannot_be_changed_after_spawn(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([ScriptedTurn(content='unused')]),
        workspace_root=tmp_path,
    )
    try:
        target = host.open_chat('.syndica.yaml')
        request = RunRequest(
            **{
                **RunRequest(prompt='go', session=target).__dict__,
                **changes,
            }
        )
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(request)
    finally:
        host.close()

    assert 'spawn' in excinfo.value.expected
    assert '忽略' in excinfo.value.received
    assert '新' in excinfo.value.next_action


def test_agent_max_turns_cannot_loosen_the_manifest_budget(tmp_path: Path) -> None:
    config = tmp_path / 'budget.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Worker, description: limited, budget: {turns: 1}}
""",
        encoding='utf-8',
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='first only',
                tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')],
            ),
            ScriptedTurn(content='must not run'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='respect the cap',
                config=str(config),
                agent='Worker',
                max_turns=2,
            )
        )
    finally:
        host.close()

    assert output == 'first only'
    assert len(llm.calls) == 1


def test_topology_max_turns_cannot_loosen_the_root_manifest_budget(
    tmp_path: Path,
) -> None:
    config = tmp_path / 'topology-budget.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Specialist, description: limited, budget: {turns: 1}}
topologies:
  limited:
    slots:
      - {name: root, agent: Specialist, is_root: true, cardinality: singleton, boot: eager}
""",
        encoding='utf-8',
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='first only',
                tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')],
            ),
            ScriptedTurn(content='must not run'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='respect the cap',
                config=str(config),
                topology='limited',
                max_turns=2,
            )
        )
    finally:
        host.close()

    assert output == 'first only'
    assert len(llm.calls) == 1


_TEAM = """
version: 1
agents:
  - {name: Worker, description: custom worker, model: worker-model}
  - {name: Explorer, description: custom explorer, model: explorer-model}
  - {name: Lead, description: lead role, model: lead-model}
"""


def _team(tmp_path: Path, extra: str = '') -> Path:
    config = tmp_path / 'team.yaml'
    config.write_text(_TEAM + extra, encoding='utf-8')
    return config


def _delegating(*roles: str, answer: str) -> ScriptedLLM:
    delegations = [
        ScriptedTurn(
            tool_calls=[
                ToolCall(
                    id=f'delegate-{index}',
                    name='Delegate',
                    arguments=json.dumps({'agent': role, 'query': f'{role} task'}),
                )
            ]
        )
        for index, role in enumerate(roles, start=1)
    ]
    return ScriptedLLM([*delegations, ScriptedTurn(content=answer)])


def _busy_child() -> ScriptedLLM:
    return ScriptedLLM(
        [
            *(
                ScriptedTurn(tool_calls=[ToolCall(id=f'busy-{n}', name='Missing', arguments='{}')])
                for n in range(1, 4)
            ),
            ScriptedTurn(content='child finished'),
        ]
    )


def _child_id(root: ScriptedLLM, call: int) -> str:
    result = root.calls[call][-1]
    assert isinstance(result.content, str)
    found = re.search(r'agent ([0-9a-f]{32})', result.content)
    assert found is not None, result.content
    return found.group(1)


def _host(tmp_path: Path, models: dict[str, ScriptedLLM], stderr: StringIO) -> RuntimeCliHost:
    return RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda name: models.get(name, models['default']),
        workspace_root=tmp_path,
    )


def test_a_failed_delegated_agent_is_reported_on_stderr_when_the_root_still_answers(
    tmp_path: Path,
) -> None:
    root = _delegating('Worker', answer='root answered anyway')
    worker = ScriptedLLM(
        [
            ScriptedTurn(
                raises=LLMResponseError(
                    expected='OpenAI 兼容端点返回一条可解析的 assistant 消息',
                    received=(
                        'HTTP 请求失败：HTTP 400 Bad Request: '
                        '"messages[3].content[0]" must be an object'
                    ),
                    next_action='检查 base_url、认证信息、模型名、请求参数和端点状态后重试',
                )
            )
        ]
    )
    stderr = StringIO()
    host = _host(tmp_path, {'default': root, 'worker-model': worker}, stderr)
    try:
        output = host.run_request(RunRequest(prompt='survey', config=str(_team(tmp_path))))
    finally:
        host.close()

    report = stderr.getvalue()
    assert output == 'root answered anyway'
    assert f'Worker {_child_id(root, 1)}' in report
    assert '"messages[3].content[0]" must be an object' in report
    assert '下一步: 检查 base_url' in report


def test_every_child_failure_is_reported_once_in_the_order_it_happened(tmp_path: Path) -> None:
    root = _delegating('Worker', 'Explorer', answer='root done')
    worker = ScriptedLLM([ScriptedTurn(raises=RuntimeError('worker exploded'))])
    explorer = ScriptedLLM([ScriptedTurn(raises=RuntimeError('explorer exploded'))])
    stderr = StringIO()
    host = _host(
        tmp_path,
        {'default': root, 'worker-model': worker, 'explorer-model': explorer},
        stderr,
    )
    try:
        output = host.run_request(RunRequest(prompt='survey', config=str(_team(tmp_path))))
    finally:
        host.close()

    report = stderr.getvalue()
    assert output == 'root done'
    assert report.count('worker exploded') == 1
    assert report.count('explorer exploded') == 1
    assert report.index('worker exploded') < report.index('explorer exploded')
    assert f'Worker {_child_id(root, 1)}' in report
    assert f'Explorer {_child_id(root, 2)}' in report


def test_a_root_failure_is_quoted_once_after_the_child_failures_before_it(
    tmp_path: Path,
) -> None:
    root = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='delegate-1',
                        name='Delegate',
                        arguments='{"agent":"Worker","query":"Worker task"}',
                    )
                ]
            ),
            ScriptedTurn(raises=RuntimeError('root exploded')),
        ]
    )
    worker = ScriptedLLM([ScriptedTurn(raises=RuntimeError('worker exploded'))])
    stderr = StringIO()
    host = _host(tmp_path, {'default': root, 'worker-model': worker}, stderr)
    try:
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='survey', config=str(_team(tmp_path))))
    finally:
        host.close()

    assert 'root exploded' in excinfo.value.received
    assert 'worker exploded' in stderr.getvalue()
    assert 'root exploded' not in stderr.getvalue()


def test_a_completed_run_writes_only_its_trace_directory_to_stderr(tmp_path: Path) -> None:
    stderr = StringIO()
    host = _host(tmp_path, {'default': ScriptedLLM([ScriptedTurn(content='done')])}, stderr)
    try:
        output = host.run_request(RunRequest(prompt='go', agent='Worker'))
    finally:
        host.close()

    assert output == 'done'
    [directory] = (tmp_path / '.syndica' / 'traces').iterdir()
    assert stderr.getvalue() == f'trace：.syndica/traces/{directory.name}\n'


def test_a_run_with_tracing_off_writes_nothing_to_stderr_and_no_trace(tmp_path: Path) -> None:
    stderr = StringIO()
    host = _host(tmp_path, {'default': ScriptedLLM([ScriptedTurn(content='done')])}, stderr)
    try:
        output = host.run_request(RunRequest(prompt='go', agent='Worker', trace='off'))
    finally:
        host.close()

    assert output == 'done'
    assert stderr.getvalue() == ''
    assert not (tmp_path / '.syndica' / 'traces').exists()


def test_a_run_that_ends_without_any_text_fails_with_its_stop_reason(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')])]
    )
    host = _host(tmp_path, {'default': llm}, StringIO())
    try:
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='one turn only', agent='Worker', max_turns=1))
    finally:
        host.close()

    assert 'stop_reason=budget' in excinfo.value.received
    assert '--max-turns' in excinfo.value.next_action
    assert len(llm.calls) == 1


def test_a_text_answer_cut_short_by_the_budget_is_flagged_on_stderr(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='partial',
                tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')],
            )
        ]
    )
    stderr = StringIO()
    host = _host(tmp_path, {'default': llm}, stderr)
    try:
        output = host.run_request(RunRequest(prompt='one turn only', agent='Worker', max_turns=1))
    finally:
        host.close()

    assert output == 'partial'
    assert '预算用完了' in stderr.getvalue()
    assert '停止原因 budget' in stderr.getvalue()
    assert '--max-turns' in stderr.getvalue()


def test_chat_refuses_a_message_to_an_agent_whose_budget_is_spent(tmp_path: Path) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='first answer'), ScriptedTurn(content='never')])
    stderr = StringIO()
    host = _host(tmp_path, {'default': llm}, stderr)
    try:
        target = host.open_chat('.syndica.yaml', max_turns=1)
        first = host.run_request(RunRequest(prompt='first', session=target))
        with pytest.raises(ConfigError) as refused:
            host.run_request(RunRequest(prompt='second', session=target))
    finally:
        host.close()

    assert first == 'first answer'
    assert 'turns' in refused.value.received and '追加预算' in refused.value.received
    assert len(llm.calls) == 1


def test_every_stop_reason_explains_a_missing_answer() -> None:
    from syndica.cli.host import _NO_ANSWER  # pyright: ignore[reportPrivateUsage]

    assert set(_NO_ANSWER) == {*StopReason, None}


def test_a_message_without_a_receipt_id_is_not_matched_to_the_initial_query() -> None:
    from syndica.cli.host import _after_message  # pyright: ignore[reportPrivateUsage]

    context = Context()
    context.append(ContextBlock(role='user', content='initial query'))
    context.append(ContextBlock(role='assistant', content='old answer'))

    assert _after_message(context, None) == 2


def test_max_turns_keeps_a_file_prompt_as_the_text_it_was_read_as(tmp_path: Path) -> None:
    (tmp_path / 'prompt.md').write_text('notes.md', encoding='utf-8')
    (tmp_path / 'notes.md').write_text('a different prompt', encoding='utf-8')
    config = tmp_path / 'prompted.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Worker, description: prompted worker, system_prompt: prompt.md}
""",
        encoding='utf-8',
    )
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    host = _host(tmp_path, {'default': llm}, StringIO())
    try:
        output = host.run_request(
            RunRequest(prompt='go', config=str(config), agent='Worker', max_turns=2)
        )
    finally:
        host.close()

    assert output == 'done'
    assert llm.calls[0][0].content == 'notes.md'


def test_max_turns_is_shared_by_the_tree_and_a_child_gets_half_of_what_the_parent_can_spare(
    tmp_path: Path,
) -> None:
    root = _delegating('Worker', answer='root done')
    worker = _busy_child()
    host = _host(tmp_path, {'default': root, 'worker-model': worker}, StringIO())
    try:
        output = host.run_request(RunRequest(prompt='go', config=str(_team(tmp_path)), max_turns=6))
    finally:
        host.close()

    delegated = next(b for b in root.calls[1] if b.tool_call_id == 'delegate-1').content
    assert output == 'root done'
    assert len(worker.calls) == 2
    assert isinstance(delegated, str)
    assert '预算' in delegated and '用完' in delegated


def test_max_turns_is_shared_by_the_tree_in_a_topology_run(tmp_path: Path) -> None:
    config = _team(
        tmp_path,
        """
topologies:
  team:
    slots:
      - {name: root, agent: Lead, is_root: true, cardinality: singleton, boot: eager}
""",
    )
    lead = _delegating('Worker', answer='lead done')
    worker = _busy_child()
    host = _host(
        tmp_path,
        {'default': ScriptedLLM([]), 'lead-model': lead, 'worker-model': worker},
        StringIO(),
    )
    try:
        output = host.run_request(
            RunRequest(prompt='go', config=str(config), topology='team', max_turns=6)
        )
    finally:
        host.close()

    assert output == 'lead done'
    assert len(worker.calls) == 2


def test_max_turns_tightens_explicit_slot_budgets_instead_of_breaking_assembly(
    tmp_path: Path,
) -> None:
    config = _team(
        tmp_path,
        """
topologies:
  team:
    slots:
      - {name: root, agent: Lead, is_root: true, cardinality: singleton, boot: eager}
      - {name: helper, agent: Worker, cardinality: singleton, boot: eager, budget: {turns: 5}}
""",
    )
    lead = ScriptedLLM([ScriptedTurn(content='lead done')])
    host = _host(tmp_path, {'default': ScriptedLLM([]), 'lead-model': lead}, StringIO())
    try:
        output = host.run_request(
            RunRequest(prompt='go', config=str(config), topology='team', max_turns=2)
        )
    finally:
        host.close()

    assert output == 'lead done'


def test_chat_max_turns_is_shared_by_every_agent_in_the_session(tmp_path: Path) -> None:
    config = _team(tmp_path)
    root = _delegating('Worker', answer='root done')
    worker = _busy_child()
    host = _host(tmp_path, {'default': root, 'worker-model': worker}, StringIO())
    try:
        target = host.open_chat(str(config), max_turns=6)
        output = host.run_request(RunRequest(prompt='go', config=str(config), session=target))
    finally:
        host.close()

    assert output == 'root done'
    assert len(worker.calls) == 2
