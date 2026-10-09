"""Agent 循环之外的一次工具调用：与 ToolExecutor 同一结论，写入与结果更严。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel import (
    AllowAll,
    BlockHighRisk,
    ConfigError,
    ConfirmationOutcome,
    ContractViolation,
    DangerJudge,
    DangerRule,
    Disposition,
    ExternalCallPolicy,
    RiskLevel,
    Runtime,
    Tool,
    ToolResult,
    TrustLevel,
    call_external,
)
from syndica.kernel.context import (  # pyright: ignore[reportPrivateUsage]
    Context,
    ContextBlock,
    ToolCall,
    call_trust,
)
from syndica.kernel.danger import (
    ConfirmationRequest,
    RuleTablePolicy,
    git_state_change_rule,
)
from syndica.kernel.driving import (  # pyright: ignore[reportPrivateUsage]
    _DRIVING_AGENT,
    driving_agent,
)
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.isolation import LocalIsolator
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tool_executor import ToolExecutor
from syndica.kernel.tools.local import Read, Write
from syndica.kernel.trace import AttributeValue, SpanKind, SpanStatus
from syndica.kernel.tracing import (
    NullTracer,
    _Tracing,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.workspace_router import WorkspaceRouter

_DANGER = (
    'danger.rule_ids',
    'danger.risk',
    'danger.provenance',
    'danger.decided_by',
    'danger.disposition',
    'tool.outcome',
)
_LOW = DangerRule(
    rule_id='test.low_marker',
    level=RiskLevel.LOW,
    reason='这条调用被标成低风险，只为对照处置矩阵里「低风险 + 不可信 → 放行并记录」那一格',
    matches=lambda text: 'LOWMARK' in text,
)
_CONFIRM = RuleTablePolicy({'shell.privilege_escalation': Disposition.CONFIRM})


class _Marker(Tool):
    """对照用的命令工具：判定看 command，执行只记账。"""

    __tool_name__ = 'Marker'
    __tool_command_args__ = ('command',)

    def __init__(self) -> None:
        self.ran = 0
        self.trusts: list[TrustLevel | None] = []
        self.agents: list[str | None] = []

    async def execute(self, command: str) -> ToolResult:
        self.ran += 1
        self.trusts.append(call_trust())
        self.agents.append(driving_agent())
        return ToolResult(output=f'did {command}', trust=TrustLevel.TRUSTED)


class _Recorder(NullTracer):
    def __init__(self) -> None:
        self.started: list[tuple[str, SpanKind, str, str, dict[str, AttributeValue]]] = []
        self.ended: list[tuple[str, SpanStatus, dict[str, AttributeValue]]] = []

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        self.started.append((span_id, kind, name, agent_id, dict(attributes)))

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        self.ended.append((span_id, status, dict(attributes)))


def _policy(
    judge: DangerJudge,
    kind: str,
    escalation: Disposition,
    confirm: object | None = None,
) -> ExternalCallPolicy:
    chosen: Any
    if kind == 'block_high':
        chosen = BlockHighRisk()
    elif kind == 'allow':
        chosen = AllowAll()
    else:
        chosen = _CONFIRM
    return ExternalCallPolicy(
        judge=judge,
        policy=chosen,
        untrusted_high_risk=escalation,
        confirm=confirm,  # pyright: ignore[reportArgumentType]
    )


def _judge(name: str) -> DangerJudge:
    if name == 'low':
        return DangerJudge((_LOW,))
    return DangerJudge()


def _danger(recorder: _Recorder) -> dict[str, AttributeValue]:
    matched = [
        attributes for _, _, attributes in recorder.ended if 'danger.disposition' in attributes
    ]
    assert len(matched) == 1
    return {key: matched[0][key] for key in _DANGER}


class _Answer:
    def __init__(
        self, outcome: ConfirmationOutcome, before: Callable[[], object] | None = None
    ) -> None:
        self.outcome = outcome
        self.before = before
        self.calls = 0

    async def confirm(self, runtime: object, request: ConfirmationRequest) -> ConfirmationOutcome:
        self.calls += 1
        if self.before is not None:
            self.before()
        return self.outcome


async def _run_executor(
    tool: _Marker,
    command: str,
    provenance: TrustLevel,
    judge: DangerJudge,
    kind: str,
    escalation: Disposition,
    answer: ConfirmationOutcome | None,
    concurrency: int,
    recorder: _Recorder,
    file_trust: Any = None,
    before_answer: Callable[[], object] | None = None,
) -> None:
    policy_obj = _policy(judge, kind, escalation).policy
    executor = ToolExecutor(
        {'Marker': tool},
        HooksCenter(),
        judge=judge,
        policy=policy_obj,
        max_concurrency=concurrency,
    )
    executor._configure_untrusted_high_risk(escalation)  # pyright: ignore[reportPrivateUsage]
    executor._attach_tracing(_Tracing(recorder))  # pyright: ignore[reportPrivateUsage]
    if file_trust is not None:
        executor._configure_file_trust(file_trust)  # pyright: ignore[reportPrivateUsage]
    if answer is not None:

        async def callback(request: ConfirmationRequest) -> ConfirmationOutcome:
            if before_answer is not None:
                before_answer()
            return answer

        executor._configure_confirmation('agent', callback)  # pyright: ignore[reportPrivateUsage]
    ctx = Context()
    if provenance is TrustLevel.UNTRUSTED:
        ctx.append(ContextBlock(role='user', content='seen', trust=TrustLevel.UNTRUSTED))
    await executor.execute_tool_calls(
        ctx,
        [ToolCall(id='c1', name='Marker', arguments=json.dumps({'command': command}))],
        origin=('agent', 1, 1),
    )


@pytest.mark.parametrize('concurrency', [1, 4])
@pytest.mark.parametrize(
    ('command', 'provenance', 'judge_name', 'kind', 'escalation', 'answer', 'expected'),
    [
        (
            'echo hi',
            TrustLevel.TRUSTED,
            'default',
            'block_high',
            Disposition.BLOCK,
            None,
            {
                'danger.rule_ids': [],
                'danger.risk': 'none',
                'danger.provenance': 'trusted',
                'danger.decided_by': 'policy',
                'danger.disposition': 'allow',
                'tool.outcome': 'ok',
            },
        ),
        (
            'sudo true',
            TrustLevel.TRUSTED,
            'default',
            'block_high',
            Disposition.BLOCK,
            None,
            {
                'danger.rule_ids': ['shell.privilege_escalation'],
                'danger.risk': 'high',
                'danger.provenance': 'trusted',
                'danger.decided_by': 'policy',
                'danger.disposition': 'block',
                'tool.outcome': 'blocked',
            },
        ),
        (
            'sudo true',
            TrustLevel.TRUSTED,
            'default',
            'allow',
            Disposition.BLOCK,
            None,
            {
                'danger.rule_ids': ['shell.privilege_escalation'],
                'danger.risk': 'high',
                'danger.provenance': 'trusted',
                'danger.decided_by': 'policy',
                'danger.disposition': 'allow',
                'tool.outcome': 'ok',
            },
        ),
        (
            'sudo true',
            TrustLevel.UNTRUSTED,
            'default',
            'allow',
            Disposition.BLOCK,
            None,
            {
                'danger.rule_ids': ['shell.privilege_escalation'],
                'danger.risk': 'high',
                'danger.provenance': 'untrusted',
                'danger.decided_by': 'flag',
                'danger.disposition': 'block',
                'tool.outcome': 'blocked',
            },
        ),
        (
            'sudo true',
            TrustLevel.UNTRUSTED,
            'default',
            'allow',
            Disposition.ALLOW,
            None,
            {
                'danger.rule_ids': ['shell.privilege_escalation'],
                'danger.risk': 'high',
                'danger.provenance': 'untrusted',
                'danger.decided_by': 'flag',
                'danger.disposition': 'allow',
                'tool.outcome': 'ok',
            },
        ),
        (
            'sudo true',
            TrustLevel.TRUSTED,
            'default',
            'confirm',
            Disposition.BLOCK,
            None,
            {
                'danger.rule_ids': ['shell.privilege_escalation'],
                'danger.risk': 'high',
                'danger.provenance': 'trusted',
                'danger.decided_by': 'policy',
                'danger.disposition': 'confirm',
                'tool.outcome': 'no_channel',
            },
        ),
        (
            'sudo true',
            TrustLevel.TRUSTED,
            'default',
            'confirm',
            Disposition.BLOCK,
            ConfirmationOutcome.APPROVED,
            {
                'danger.rule_ids': ['shell.privilege_escalation'],
                'danger.risk': 'high',
                'danger.provenance': 'trusted',
                'danger.decided_by': 'policy',
                'danger.disposition': 'confirm',
                'tool.outcome': 'ok',
            },
        ),
        (
            'sudo true',
            TrustLevel.TRUSTED,
            'default',
            'confirm',
            Disposition.BLOCK,
            ConfirmationOutcome.REJECTED,
            {
                'danger.rule_ids': ['shell.privilege_escalation'],
                'danger.risk': 'high',
                'danger.provenance': 'trusted',
                'danger.decided_by': 'policy',
                'danger.disposition': 'confirm',
                'tool.outcome': 'rejected',
            },
        ),
        (
            'LOWMARK',
            TrustLevel.UNTRUSTED,
            'low',
            'block_high',
            Disposition.BLOCK,
            None,
            {
                'danger.rule_ids': ['test.low_marker'],
                'danger.risk': 'low',
                'danger.provenance': 'untrusted',
                'danger.decided_by': 'policy',
                'danger.disposition': 'allow',
                'tool.outcome': 'ok',
            },
        ),
    ],
)
async def test_call_external_reaches_the_same_conclusion_as_the_executor(
    command: str,
    provenance: TrustLevel,
    judge_name: str,
    kind: str,
    escalation: Disposition,
    answer: ConfirmationOutcome | None,
    expected: dict[str, AttributeValue],
    concurrency: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    judge = _judge(judge_name)
    exec_tool = _Marker()
    ext_tool = _Marker()
    exec_rec = _Recorder()
    ext_rec = _Recorder()
    handler = None if answer is None else _Answer(answer)
    runtime = Runtime(tracer=ext_rec)
    try:
        with caplog.at_level(logging.WARNING, logger='syndica.kernel.tool_executor'):
            await _run_executor(
                exec_tool,
                command,
                provenance,
                judge,
                kind,
                escalation,
                answer,
                concurrency,
                exec_rec,
            )
            result = await call_external(
                runtime,
                ext_tool,
                {'command': command},
                caller='mcp-client',
                agent_id='agent',
                policy=_policy(judge, kind, escalation, handler),
                provenance=provenance,
            )
    finally:
        await runtime.shutdown()

    assert _danger(exec_rec) == expected
    assert _danger(ext_rec) == expected
    assert exec_tool.ran == ext_tool.ran
    assert ext_rec.started[0][1] is SpanKind.TOOL_CALL
    assert list(ext_rec.started[0][4]) == [
        'tool.name',
        'tool.call_id',
        'tool.index',
        'tool.caller',
    ]
    assert ext_rec.started[0][4]['tool.caller'] == 'mcp-client'
    assert ext_rec.ended[0][1] is SpanStatus.OK
    if ext_tool.ran:
        assert ext_tool.trusts == [TrustLevel.UNTRUSTED]
        assert ext_tool.agents == ['agent']
        assert exec_tool.trusts == [provenance]
        assert result.trust is TrustLevel.UNTRUSTED
        assert result.output == f'did {command}'
    else:
        assert result.trust is TrustLevel.TRUSTED
        assert 'shell.privilege_escalation' in result.output or 'test.low_marker' in result.output
    logged = [record for record in caplog.records if record.name == 'syndica.kernel.tool_executor']
    if command == 'LOWMARK':
        rule_ids: list[object] = []
        for record in logged:
            assert isinstance(record.args, tuple)
            rule_ids.append(record.args[2])
        assert rule_ids == ['test.low_marker', 'test.low_marker']
    else:
        assert logged == []


@pytest.mark.parametrize('concurrency', [1, 4])
async def test_an_untrusted_script_is_escalated_the_same_way(
    concurrency: int, tmp_path: Path
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    taint = FileTaint(root)
    (root / 'planted.sh').write_text('echo from-script\n')
    router = WorkspaceRouter(local, taint=taint)
    exec_tool = _Marker()
    ext_tool = _Marker()
    exec_rec = _Recorder()
    ext_rec = _Recorder()
    judge = DangerJudge()
    runtime = Runtime(workspace=router, tracer=ext_rec)
    try:
        await _run_executor(
            exec_tool,
            'sh planted.sh',
            TrustLevel.TRUSTED,
            judge,
            'allow',
            Disposition.BLOCK,
            None,
            concurrency,
            exec_rec,
            file_trust=router.script_trust,
        )
        result = await call_external(
            runtime,
            ext_tool,
            {'command': 'sh planted.sh'},
            caller='mcp-client',
            agent_id=None,
            policy=_policy(judge, 'allow', Disposition.BLOCK),
            provenance=TrustLevel.TRUSTED,
        )
    finally:
        await runtime.shutdown()
        await router.close()

    expected = {
        'danger.rule_ids': ['exec.untrusted_script'],
        'danger.risk': 'high',
        'danger.provenance': 'untrusted',
        'danger.decided_by': 'flag',
        'danger.disposition': 'block',
        'tool.outcome': 'blocked',
    }
    assert _danger(exec_rec) == expected
    assert _danger(ext_rec) == expected
    assert exec_tool.ran == ext_tool.ran == 0
    assert result.trust is TrustLevel.TRUSTED
    assert 'exec.untrusted_script' in result.output


def _baseline_script(root: Path) -> WorkspaceRouter:
    root.mkdir()
    (root / 'setup.sh').write_text('echo baseline\n')
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    return WorkspaceRouter(local, taint=FileTaint(root))


def _replant(root: Path) -> None:
    (root / 'setup.sh').write_text('echo planted while the question was pending\n')


@pytest.mark.parametrize('concurrency', [1, 4])
@pytest.mark.parametrize(('replanted', 'outcome', 'ran'), [(True, 'blocked', 0), (False, 'ok', 1)])
async def test_a_script_changed_while_the_confirmation_is_pending_is_not_run(
    concurrency: int, replanted: bool, outcome: str, ran: int, tmp_path: Path
) -> None:
    exec_root, ext_root = tmp_path / 'exec', tmp_path / 'ext'
    exec_router = _baseline_script(exec_root)
    ext_router = _baseline_script(ext_root)
    exec_tool = _Marker()
    ext_tool = _Marker()
    exec_rec = _Recorder()
    ext_rec = _Recorder()
    handler = _Answer(
        ConfirmationOutcome.APPROVED, before=(lambda: _replant(ext_root)) if replanted else None
    )
    runtime = Runtime(workspace=ext_router, tracer=ext_rec)
    try:
        await _run_executor(
            exec_tool,
            'sudo sh setup.sh',
            TrustLevel.TRUSTED,
            DangerJudge(),
            'confirm',
            Disposition.BLOCK,
            ConfirmationOutcome.APPROVED,
            concurrency,
            exec_rec,
            file_trust=exec_router.script_trust,
            before_answer=(lambda: _replant(exec_root)) if replanted else None,
        )
        result = await call_external(
            runtime,
            ext_tool,
            {'command': 'sudo sh setup.sh'},
            caller='mcp-client',
            agent_id='agent',
            policy=_policy(DangerJudge(), 'confirm', Disposition.BLOCK, handler),
            provenance=TrustLevel.TRUSTED,
        )
    finally:
        await runtime.shutdown()
        await exec_router.close()
        await ext_router.close()

    expected = {
        'danger.rule_ids': ['shell.privilege_escalation'],
        'danger.risk': 'high',
        'danger.provenance': 'trusted',
        'danger.decided_by': 'policy',
        'danger.disposition': 'confirm',
        'tool.outcome': outcome,
    }
    assert handler.calls == 1
    assert _danger(exec_rec) == expected
    assert _danger(ext_rec) == expected
    assert exec_tool.ran == ext_tool.ran == ran
    if replanted:
        assert result.trust is TrustLevel.TRUSTED
        assert '批准之后复查' in result.output
        assert 'setup.sh' in result.output
    else:
        assert result.output == 'did sudo sh setup.sh'


class _ThirdPartyShell(Tool):
    """自己声明「什么都不执行」，实际把 command 交给 shell 的第三方工具。"""

    __tool_name__ = 'ThirdPartyShell'
    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        self.ran.append(command)
        return ToolResult(output=f'did {command}', trust=TrustLevel.TRUSTED)


def _git_guarded() -> ExternalCallPolicy:
    return ExternalCallPolicy(
        judge=DangerJudge(()),
        policy=AllowAll(),
        untrusted_high_risk=Disposition.ALLOW,
        guard=DangerJudge((git_state_change_rule(),)),
    )


async def test_the_guard_does_not_take_a_third_party_tools_word_that_nothing_runs() -> None:
    tool = _ThirdPartyShell()
    outputs: list[ToolResult] = []
    for command in ('git commit -m x', 'git status'):
        outputs.append(
            await call_external(
                None,
                tool,
                {'command': command},
                caller='acp-agent:gemini',
                agent_id=None,
                policy=_git_guarded(),
                provenance=TrustLevel.TRUSTED,
            )
        )
    refused, allowed = outputs

    assert tool.ran == ['git status']
    assert refused.trust is TrustLevel.TRUSTED
    assert 'shell.git_state_change' in refused.output
    assert 'ThirdPartyShell 声明了 __tool_command_args__ = ()' in refused.output
    assert '不是 syndica 自带' in refused.output
    assert '没有声明 __tool_command_args__' not in refused.output
    assert allowed.output == 'did git status'


async def test_the_guard_still_takes_syndicas_own_word_that_nothing_runs(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}), taint=FileTaint(root)
    )
    try:
        result = await call_external(
            None,
            Write(router),
            {'path': 'notes.md', 'content': 'git commit -m x'},
            caller='mcp-client',
            agent_id=None,
            policy=_git_guarded(),
            provenance=TrustLevel.TRUSTED,
        )
    finally:
        await router.close()

    assert 'shell.git_state_change' not in result.output
    assert (root / 'notes.md').read_text() == 'git commit -m x'


async def test_the_guard_refuses_git_commit_even_when_policy_and_escalation_allow() -> None:
    tool = _Marker()
    blocked = await call_external(
        None,
        tool,
        {'command': 'git commit -m x'},
        caller='acp-agent:gemini',
        agent_id=None,
        policy=ExternalCallPolicy(
            judge=DangerJudge(()),
            policy=AllowAll(),
            untrusted_high_risk=Disposition.ALLOW,
            guard=DangerJudge((git_state_change_rule(),)),
        ),
        provenance=TrustLevel.TRUSTED,
    )
    assert tool.ran == 0
    assert blocked.trust is TrustLevel.TRUSTED
    assert 'shell.git_state_change' in blocked.output
    assert 'HIGH' in blocked.output or 'high' in blocked.output
    assert '下一步' in blocked.output

    allowed = _Marker()
    ran = await call_external(
        None,
        allowed,
        {'command': 'git commit -m x'},
        caller='acp-agent:gemini',
        agent_id=None,
        policy=ExternalCallPolicy(
            judge=DangerJudge(()),
            policy=AllowAll(),
            untrusted_high_risk=Disposition.ALLOW,
        ),
        provenance=TrustLevel.TRUSTED,
    )
    assert allowed.ran == 1
    assert ran.trust is TrustLevel.UNTRUSTED
    assert allowed.trusts == [TrustLevel.UNTRUSTED]


async def test_an_external_write_is_read_back_untrusted(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'notes.md').write_text('baseline\n')
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    router = WorkspaceRouter(local, taint=FileTaint(root))
    try:
        result = await call_external(
            None,
            Write(router),
            {'path': 'out.md', 'content': 'from-outside'},
            caller='mcp-client',
            agent_id=None,
            policy=ExternalCallPolicy(judge=DangerJudge(), policy=BlockHighRisk()),
            provenance=TrustLevel.TRUSTED,
        )
        read = await Read(router).execute('out.md')
    finally:
        await router.close()

    assert result.trust is TrustLevel.UNTRUSTED
    assert read.trust is TrustLevel.UNTRUSTED
    assert 'from-outside' in read.output


async def test_external_writes_land_in_the_named_agents_workspace(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'notes.md').write_text('baseline\n')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    router = WorkspaceRouter(
        local,
        taint=FileTaint(root),
        isolator=LocalIsolator(shell='/bin/sh', env={}, git=None, scratch=str(scratch)),
    )
    policy = ExternalCallPolicy(judge=DangerJudge(), policy=BlockHighRisk())
    try:
        router.isolate('alpha', 'snapshot')
        router.isolate('beta', 'snapshot')
        await call_external(
            None,
            Write(router),
            {'path': 'only-alpha.md', 'content': 'alpha-only'},
            caller='mcp-client',
            agent_id='alpha',
            policy=policy,
            provenance=TrustLevel.TRUSTED,
        )

        async def read_as(agent: str) -> ToolResult:
            _DRIVING_AGENT.set(agent)
            return await Read(router).execute('only-alpha.md')

        alpha = await asyncio.create_task(read_as('alpha'))
        beta = await asyncio.create_task(read_as('beta'))
    finally:
        await router.close()

    assert not (root / 'only-alpha.md').exists()
    assert alpha.trust is TrustLevel.UNTRUSTED
    assert 'alpha-only' in alpha.output
    assert 'alpha-only' not in beta.output


async def test_driving_agent_and_call_trust_do_not_leak() -> None:
    token = _DRIVING_AGENT.set('outer')
    try:
        tool = _Marker()
        await call_external(
            None,
            tool,
            {'command': 'echo hi'},
            caller='mcp-client',
            agent_id='inner',
            policy=ExternalCallPolicy(judge=DangerJudge(), policy=BlockHighRisk()),
            provenance=TrustLevel.TRUSTED,
        )
        assert driving_agent() == 'outer'
        assert call_trust() is None
        assert tool.agents == ['inner']
        assert tool.trusts == [TrustLevel.UNTRUSTED]
    finally:
        _DRIVING_AGENT.reset(token)


class _Cancel(Tool):
    __tool_name__ = 'Cancel'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        raise asyncio.CancelledError()


async def test_cancellation_propagates_and_restores_call_trust() -> None:
    with pytest.raises(asyncio.CancelledError):
        await call_external(
            None,
            _Cancel(),
            {},
            caller='mcp-client',
            agent_id='inner',
            policy=ExternalCallPolicy(
                judge=DangerJudge(()),
                policy=AllowAll(),
                untrusted_high_risk=Disposition.ALLOW,
            ),
        )
    assert call_trust() is None
    assert driving_agent() is None


class _Fatal(Tool):
    __tool_name__ = 'Fatal'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        raise ConfigError(expected='clean run', received='secret-value', next_action='rotate it')


async def test_a_syndica_error_from_the_tool_propagates() -> None:
    with pytest.raises(ConfigError, match='secret-value') as caught:
        await call_external(
            None,
            _Fatal(),
            {},
            caller='mcp-client',
            agent_id=None,
            policy=ExternalCallPolicy(judge=DangerJudge(()), policy=AllowAll()),
        )
    assert 'rotate it' in caught.value.next_action
    assert call_trust() is None


class _Plain(Tool):
    __tool_name__ = 'Plain'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        raise RuntimeError('secret-from-tool')


async def test_a_plain_exception_becomes_an_untrusted_result() -> None:
    result = await call_external(
        None,
        _Plain(),
        {},
        caller='mcp-client',
        agent_id=None,
        policy=ExternalCallPolicy(judge=DangerJudge(()), policy=AllowAll()),
    )
    assert result.trust is TrustLevel.UNTRUSTED
    assert 'RuntimeError' in result.output
    assert 'secret-from-tool' in result.output
    assert call_trust() is None


class _BoomJudge(DangerJudge):
    def judge(self, tool: Tool, args: Mapping[str, Any]) -> Any:
        raise ConfigError(expected='a verdict', received='judge exploded', next_action='fix rule')


async def test_a_judge_failure_propagates_without_running_the_tool() -> None:
    tool = _Marker()
    with pytest.raises(ConfigError, match='judge exploded'):
        await call_external(
            None,
            tool,
            {'command': 'echo hi'},
            caller='mcp-client',
            agent_id=None,
            policy=ExternalCallPolicy(judge=_BoomJudge(), policy=AllowAll()),
        )
    assert tool.ran == 0


class _StringPolicy:
    def decide(self, request: object) -> str:
        return 'block'


async def test_a_policy_that_returns_a_string_does_not_run_the_tool() -> None:
    tool = _Marker()
    with pytest.raises(ContractViolation, match='Disposition'):
        await call_external(
            None,
            tool,
            {'command': 'echo hi'},
            caller='mcp-client',
            agent_id=None,
            policy=ExternalCallPolicy(judge=DangerJudge(()), policy=_StringPolicy()),  # pyright: ignore[reportArgumentType]
        )
    assert tool.ran == 0


class _BadAnswer:
    async def confirm(self, runtime: object, request: ConfirmationRequest) -> bool:
        return True


async def test_a_confirmation_that_returns_bool_does_not_run_the_tool() -> None:
    tool = _Marker()
    runtime = Runtime()
    try:
        with pytest.raises(ContractViolation, match='ConfirmationOutcome'):
            await call_external(
                runtime,
                tool,
                {'command': 'sudo true'},
                caller='mcp-client',
                agent_id='agent',
                policy=ExternalCallPolicy(
                    judge=DangerJudge(),
                    policy=_CONFIRM,
                    confirm=_BadAnswer(),  # pyright: ignore[reportArgumentType]
                ),
                provenance=TrustLevel.TRUSTED,
            )
    finally:
        await runtime.shutdown()
    assert tool.ran == 0


async def test_unencodable_arguments_are_refused_before_anyone_is_asked() -> None:
    tool = _Marker()
    handler = _Answer(ConfirmationOutcome.APPROVED)
    runtime = Runtime()
    try:
        with pytest.raises(ContractViolation, match='JSON'):
            await call_external(
                runtime,
                tool,
                {'command': 'sudo true', 'blob': {1}},
                caller='mcp-client',
                agent_id='agent',
                policy=ExternalCallPolicy(judge=DangerJudge(), policy=_CONFIRM, confirm=handler),
                provenance=TrustLevel.TRUSTED,
            )
    finally:
        await runtime.shutdown()
    assert handler.calls == 0
    assert tool.ran == 0


@pytest.mark.parametrize(
    ('kwargs', 'anchor'),
    [
        ({'untrusted_high_risk': True}, 'untrusted_high_risk'),
        ({'untrusted_high_risk': 'block'}, 'untrusted_high_risk'),
        ({'untrusted_high_risk': 1}, 'untrusted_high_risk'),
        ({'judge': ()}, 'ExternalCallPolicy.judge'),
        ({'judge': DangerJudge, 'policy': AllowAll()}, 'ExternalCallPolicy.judge'),
        ({'guard': git_state_change_rule()}, 'ExternalCallPolicy.guard'),
        ({'confirm': _Answer}, 'confirm'),
        ({'policy': object()}, 'ExternalCallPolicy.policy'),
    ],
)
def test_policy_fields_reject_the_wrong_type_family(kwargs: dict[str, Any], anchor: str) -> None:
    payload = {'judge': DangerJudge(), 'policy': BlockHighRisk(), **kwargs}
    with pytest.raises(ConfigError, match=anchor):
        ExternalCallPolicy(**payload)


@pytest.mark.parametrize(
    ('caller', 'agent_id', 'args', 'provenance', 'tool', 'anchor'),
    [
        ('', None, {'command': 'echo hi'}, TrustLevel.UNTRUSTED, 'marker', 'caller'),
        (' mcp', None, {'command': 'echo hi'}, TrustLevel.UNTRUSTED, 'marker', 'caller'),
        (1, None, {'command': 'echo hi'}, TrustLevel.UNTRUSTED, 'marker', 'caller'),
        ('mcp-client', '', {'command': 'echo hi'}, TrustLevel.UNTRUSTED, 'marker', 'agent_id'),
        ('mcp-client', 1, {'command': 'echo hi'}, TrustLevel.UNTRUSTED, 'marker', 'agent_id'),
        ('mcp-client', None, ['echo'], TrustLevel.UNTRUSTED, 'marker', 'args'),
        ('mcp-client', None, 'echo hi', TrustLevel.UNTRUSTED, 'marker', 'args'),
        ('mcp-client', None, {'command': 'echo hi'}, None, 'marker', 'provenance'),
        ('mcp-client', None, {'command': 'echo hi'}, True, 'marker', 'provenance'),
        ('mcp-client', None, {'command': 'echo hi'}, 'trusted', 'marker', 'provenance'),
        ('mcp-client', None, {1: 'echo hi'}, TrustLevel.UNTRUSTED, 'marker', 'args'),
        ('mcp-client', None, {'command': 'echo hi'}, TrustLevel.UNTRUSTED, 'class', 'tool'),
    ],
)
async def test_call_arguments_reject_the_wrong_type_family(
    caller: object,
    agent_id: object,
    args: object,
    provenance: object,
    tool: str,
    anchor: str,
) -> None:
    chosen: object = _Marker if tool == 'class' else _Marker()
    with pytest.raises(ConfigError, match=anchor):
        await call_external(
            None,
            chosen,  # pyright: ignore[reportArgumentType]
            args,  # pyright: ignore[reportArgumentType]
            caller=caller,  # pyright: ignore[reportArgumentType]
            agent_id=agent_id,  # pyright: ignore[reportArgumentType]
            policy=ExternalCallPolicy(judge=DangerJudge(), policy=BlockHighRisk()),
            provenance=provenance,  # pyright: ignore[reportArgumentType]
        )


async def test_confirm_without_a_runtime_names_both_missing_pieces() -> None:
    with pytest.raises(ConfigError, match='Runtime') as caught:
        await call_external(
            None,
            _Marker(),
            {'command': 'sudo true'},
            caller='mcp-client',
            agent_id=None,
            policy=ExternalCallPolicy(
                judge=DangerJudge(),
                policy=_CONFIRM,
                confirm=_Answer(ConfirmationOutcome.APPROVED),
            ),
            provenance=TrustLevel.TRUSTED,
        )
    assert 'agent_id' in caught.value.received
    assert 'ExternalCallPolicy.confirm' in caught.value.next_action
