"""Declarative L2 scenario replay over the real Syndica kernel."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from syndica.kernel import (
    AgentCatalog,
    AgentFactory,
    AgentStatus,
    BaseAgent,
    ConfigError,
    ContextOverflowError,
    EventBus,
    HooksCenter,
    HostFacts,
    HumanInteractionModule,
    Junction,
    LLMResponseError,
    LocalWorkspace,
    MailboxConfirmationHandler,
    ManifestSource,
    RiskLevel,
    Runtime,
    SafetyModule,
    ScriptedLLM,
    ScriptedTurn,
    Subscription,
    ToolCall,
    Topology,
    TrustLevel,
    local_tools,
    register_builtins,
)
from syndica.kernel.danger import DangerRequest, Disposition
from syndica.kernel.errors import ContractViolation
from syndica.kernel.manifest import AgentManifest
from syndica.kernel.scenario_schema import (
    ScenarioDocument,
    ScenarioError,
    ScenarioExpectation,
    ScenarioReply,
    ScenarioRole,
    parse_scenario_document,
)
from syndica.runner.yaml import load_document_mapping

__all__ = [
    'Scenario',
    'ScenarioAgent',
    'ScenarioEvent',
    'ScenarioExpectation',
    'ScenarioMismatch',
    'ScenarioResponse',
    'ScenarioResult',
    'ScenarioRunner',
    'load_document',
    'load_scenario',
]


class ScenarioMismatch(ContractViolation):
    """A replay completed but did not satisfy its declarative expectation."""


@dataclass(frozen=True)
class ScenarioAgent:
    manifest: AgentManifest
    instances: tuple[tuple[ScriptedTurn, ...], ...]


@dataclass(frozen=True)
class ScenarioResponse:
    kind: str
    agent: str
    answer: str
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Scenario:
    version: int
    name: str
    catalog: AgentCatalog
    agents: Mapping[str, ScenarioAgent]
    topology: Topology
    entry_slot: str
    prompt: str
    responses: tuple[ScenarioResponse, ...]
    expect: ScenarioExpectation
    source: str


@dataclass(frozen=True)
class ScenarioEvent:
    sequence: int
    kind: str
    agent: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    final_output: Mapping[str, str]
    tool_order: Mapping[str, tuple[str, ...]]
    stop_reasons: Mapping[str, str]
    errors: Mapping[str, str]
    statuses: Mapping[str, str]
    events: tuple[ScenarioEvent, ...]
    workspace: Mapping[str, str]


def _error(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def load_document(path: object) -> ScenarioDocument:
    """读一份场景文档并交给内核的校验（`syndica.kernel.scenario_schema`）：`.yaml` / `.yml` 是手写的
    （PyYAML 的 safe 读法，重复键报错），`.json` 是捕获的（标准库 `json`，重复键与 NaN 报错）。两种
    读法交出同样的 Mapping，读出同样的文档。"""
    raw, file = load_document_mapping(path)
    return parse_scenario_document(raw, source=str(file))


def load_scenario(path: object) -> Scenario:
    """Parse one v1 scenario without importing any backend."""
    document = load_document(path)
    if document.version != 1:
        raise _error(
            'load_scenario 读的是 v1 手写脚本（version: 1）',
            f'{document.source!r} 是 version: {document.version} 的回放文档',
            '用 syndica.runner.load_document 读它',
        )
    file = Path(document.source)
    catalog = AgentCatalog()
    register_builtins(catalog)
    agents = {role.name: _agent(role, catalog, file) for role in document.roles.values()}
    topology = cast(Topology, document.topology)
    topology.validate(catalog)
    return Scenario(
        version=1,
        name=document.name,
        catalog=catalog,
        agents=MappingProxyType(agents),
        topology=topology,
        entry_slot=cast(str, document.entry.slot),
        prompt=cast(str, document.entry.prompt),
        responses=tuple(
            ScenarioResponse(
                kind=response.kind,
                agent=cast(str, response.agent),
                answer=cast(str, response.answer),
                tool_call_id=response.tool_call_id,
            )
            for response in document.responses
        ),
        expect=document.expect,
        source=document.source,
    )


def _agent(role: ScenarioRole, catalog: AgentCatalog, file: Path) -> ScenarioAgent:
    manifest = catalog.load_mapping(
        role.manifest,
        source=ManifestSource(
            kind='yaml_file',
            location=f'{str(file)}#agents.{role.name}.manifest',
            origin=str(file.parent.resolve()),
        ),
    )
    if manifest.name != role.name:
        raise _error(
            f'agents 键与 manifest.name 都是 {role.name!r}',
            f'manifest.name={manifest.name!r}',
            '让两处名字逐字相同；稳定 Agent selector 使用外层角色名',
        )
    instances = tuple(
        tuple(_scripted(reply) for reply in instance.replies) for instance in role.instances
    )
    return ScenarioAgent(manifest=manifest, instances=instances)


def _scripted(reply: ScenarioReply) -> ScriptedTurn:
    """v1 的一次回复：正文是内联的字符串（v1 没有 payload 引用），失败按三种 kind 之一重建。"""
    calls = [
        ToolCall(id=call.id, name=call.name, arguments=call.arguments) for call in reply.tool_calls
    ]
    raised = None if reply.raises is None else _v1_exception(reply.raises)
    return ScriptedTurn(content=cast(str | None, reply.content), tool_calls=calls, raises=raised)


def _v1_exception(error: ScenarioError) -> BaseException:
    message = cast(str, error.message)
    if error.type == 'syndica.kernel.errors.ContextOverflowError':
        return ContextOverflowError(
            expected='场景脚本指定一次 context overflow',
            received=message,
            next_action='由被测 overflow hook 处理，或把 expect.stop_reasons 设为 error',
        )
    if error.type == 'syndica.kernel.errors.LLMResponseError':
        return LLMResponseError(
            expected='场景脚本指定一次 LLM response error',
            received=message,
            next_action='由被测 llm_failure hook 处理，或把 expect.stop_reasons 设为 error',
        )
    return RuntimeError(message)


class _ScenarioPolicy:
    def __init__(self, confirmation_ids: frozenset[str]) -> None:
        self._confirmation_ids = confirmation_ids

    def decide(self, request: DangerRequest) -> Disposition:
        if request.call.id in self._confirmation_ids:
            return Disposition.CONFIRM
        if request.verdict.level is RiskLevel.HIGH:
            return Disposition.BLOCK
        return Disposition.ALLOW


def _workspace_contents(root: Path) -> dict[str, str]:
    contents: dict[str, str] = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            contents[relative] = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            raise ScenarioMismatch(
                expected='L2 v1 workspace 快照中的每个文件都可按 UTF-8 读取',
                received=f'读取 {relative!r} 失败：{type(exc).__name__}: {exc}',
                next_action='删除二进制产物，或不要在 expect.workspace 中使用 L2 v1 文本快照',
            ) from exc
    return contents


class ScenarioRunner:
    """Run one validated scenario and assert its declared result."""

    async def run(
        self,
        scenario: Scenario,
        *,
        workspace_root: str | Path | None = None,
    ) -> ScenarioResult:
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if workspace_root is None:
            temporary = tempfile.TemporaryDirectory(prefix='syndica-scenario-')
            root = Path(temporary.name)
        else:
            root = Path(workspace_root)
            root.mkdir(parents=True, exist_ok=True)
        workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
        tools = local_tools(workspace.portal())
        roles = scenario.agents
        model_names = {scenario.catalog.get(name).model for name in scenario.catalog.names()}
        placeholders = {name: ScriptedLLM([]) for name in model_names}
        base_factories = scenario.catalog.freeze(
            tools,
            placeholders,
            HostFacts(os='scenario', platform='scenario', shell='/bin/sh'),
        )
        confirmation_ids = frozenset(
            response.tool_call_id
            for response in scenario.responses
            if response.kind == 'confirmation' and response.tool_call_id is not None
        )
        policy = _ScenarioPolicy(confirmation_ids)
        selector_by_id: dict[str, str] = {}
        errors: dict[str, str] = {}
        counters = {role: 0 for role in roles}
        factories = {
            role: self._factory(
                role,
                agent,
                base_factories[role],
                policy,
                counters,
                selector_by_id,
                errors,
            )
            for role, agent in roles.items()
        }
        runtime = Runtime(
            agents=factories,
            orchestration=True,
            modules=[SafetyModule(), HumanInteractionModule()],
            flags={'untrusted_high_risk': 'ask'},
            confirmation_handler=MailboxConfirmationHandler(timeout=300),
        )
        subscription = runtime.events.subscribe()
        events: list[ScenarioEvent] = []
        response_errors: list[ScenarioMismatch] = []
        response_index = 0

        async def collect() -> None:
            nonlocal response_index
            async for event in subscription:
                selector = selector_by_id.get(event.agent_id, f'<unknown:{event.agent_id}>')
                events.append(
                    ScenarioEvent(
                        sequence=event.sequence,
                        kind=event.kind,
                        agent=selector,
                        payload=MappingProxyType(dict(event.payload)),
                    )
                )
                kind = {
                    'user_input_requested': 'user_input',
                    'user_question_requested': 'user_question',
                    'tool_confirmation_requested': 'confirmation',
                }.get(event.kind)
                if kind is None:
                    continue
                response = (
                    scenario.responses[response_index]
                    if response_index < len(scenario.responses)
                    else None
                )
                if (
                    response is None
                    or response.kind != kind
                    or response.agent != selector
                    or (
                        response.tool_call_id is not None
                        and response.tool_call_id != event.payload.get('tool_call_id')
                    )
                ):
                    response_errors.append(
                        ScenarioMismatch(
                            expected=(
                                '下一条 responses 与当前人机请求的 kind / agent / tool_call_id 匹配'
                            ),
                            received=(
                                f'请求 kind={kind!r} agent={selector!r} '
                                f'tool_call_id={event.payload.get("tool_call_id")!r}，'
                                f'响应脚本是 {response!r}'
                            ),
                            next_action=(
                                '修正 responses 的顺序与选择器；动态 request_id 不写进 YAML'
                            ),
                        )
                    )
                    answer = 'reject' if kind == 'confirmation' else 'unmatched response'
                else:
                    response_index += 1
                    answer = response.answer
                delivered = await runtime.send(
                    event.agent_id,
                    answer,
                    activate=False,
                    sender='human',
                    correlation_id=cast(str, event.payload['request_id']),
                    trust=TrustLevel.TRUSTED,
                )
                if not delivered.ok:
                    response_errors.append(
                        ScenarioMismatch(
                            expected='预置人类答复成功投递到等待中的 Agent',
                            received=delivered.reason or 'broker 未给出失败原因',
                            next_action='检查场景 Agent 生命周期与 request_id 路由',
                        )
                    )

        collector = asyncio.create_task(collect(), name=f'scenario-events:{scenario.name}')
        shutdown = False
        try:
            slot_ids: dict[str, str] = {}
            for slot in scenario.topology.slots:
                if slot.boot != 'eager':
                    continue
                slot_ids[slot.name] = await runtime.spawn(
                    factories[slot.agent],
                    exit_mode=slot.exit_mode,
                    metadata=dict(slot.metadata),
                )
            entry_slot = next(
                slot for slot in scenario.topology.slots if slot.name == scenario.entry_slot
            )
            root_id = slot_ids.get(entry_slot.name)
            if root_id is None:
                root_id = await runtime.spawn(
                    factories[entry_slot.agent],
                    exit_mode=entry_slot.exit_mode,
                    metadata=dict(entry_slot.metadata),
                )
                slot_ids[entry_slot.name] = root_id
            delivered = await runtime.send(root_id, scenario.prompt)
            if not delivered.ok:
                raise ScenarioMismatch(
                    expected=f'entry.slot={scenario.entry_slot!r} 接受初始消息',
                    received=delivered.reason or 'broker 未给出失败原因',
                    next_action='检查 entry.slot、拓扑 boot/cardinality 与根 Agent 生命周期',
                )
            await runtime.wait(root_id)
            await runtime.shutdown()
            shutdown = True
            await collector
            result = await self._result(
                scenario,
                runtime,
                selector_by_id,
                events,
                errors,
                subscription,
                root,
            )
        finally:
            if not shutdown:
                await runtime.shutdown()
            if not collector.done():
                await collector
            await workspace.close()
            subscription.close()
            if temporary is not None:
                temporary.cleanup()
        if response_errors:
            raise response_errors[0]
        if response_index != len(scenario.responses):
            raise ScenarioMismatch(
                expected='responses 中每一项都被一次人机请求消费',
                received=(
                    f'消费了 {response_index} / {len(scenario.responses)} 项，'
                    f'剩余 {scenario.responses[response_index:]!r}'
                ),
                next_action=(
                    '删除多余响应，或补上会触发对应 AskUser / confirmation 的 scripted turn'
                ),
            )
        self._assert_result(scenario.expect, result)
        return result

    def _factory(
        self,
        role: str,
        scenario_agent: ScenarioAgent,
        base_factory: AgentFactory,
        policy: _ScenarioPolicy,
        counters: dict[str, int],
        selector_by_id: dict[str, str],
        errors: dict[str, str],
    ) -> AgentFactory:
        template = base_factory(HooksCenter(), EventBus()).options

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            index = counters[role]
            if index >= len(scenario_agent.instances):
                raise ConfigError(
                    expected=(
                        f'agents.{role}.instances 至少有 {index + 1} 份脚本，'
                        '每个实例消费独立 ScriptedLLM'
                    ),
                    received=f'只提供了 {len(scenario_agent.instances)} 份',
                    next_action=(
                        f'补 agents.{role}.instances[{index}].turns；不要复用上一实例的脚本或游标'
                    ),
                )
            counters[role] += 1
            options = replace(
                template,
                llm=ScriptedLLM(list(scenario_agent.instances[index])),
                tools=dict(template.tools),
                danger_policy=policy,
            )
            agent = BaseAgent(options, hooks=hooks, events=events)
            selector = f'{role}[{index}]'
            selector_by_id[agent.agent_id] = selector

            async def capture_failure(**kwargs: object) -> None:
                error = kwargs.get('error')
                if isinstance(error, BaseException):
                    errors.setdefault(selector, f'{type(error).__name__}: {error}')

            agent.register_hook(Junction.LLM_FAILURE, capture_failure)
            return agent

        return build

    async def _result(
        self,
        scenario: Scenario,
        runtime: Runtime,
        selector_by_id: Mapping[str, str],
        events: list[ScenarioEvent],
        errors: Mapping[str, str],
        subscription: Subscription,
        root: Path,
    ) -> ScenarioResult:
        infos = await runtime.registry.list()
        statuses: dict[str, str] = {}
        final_output: dict[str, str] = {}
        tool_order: dict[str, tuple[str, ...]] = {}
        stop_reasons: dict[str, str] = {}
        for info in infos:
            selector = selector_by_id.get(info.agent_id)
            if selector is None:
                continue
            statuses[selector] = info.status.value
            if info.status is not AgentStatus.ZOMBIE:
                continue
            context = await runtime.wait(info.agent_id)
            assistants = [
                block.content
                for block in context.current_traj
                if block.role == 'assistant' and isinstance(block.content, str)
            ]
            if assistants:
                final_output[selector] = assistants[-1]
            tool_order[selector] = tuple(
                call.name
                for block in context.current_traj
                if block.role == 'assistant'
                for call in block.tool_calls
            )
            if context.stop_reason is not None:
                stop_reasons[selector] = context.stop_reason.value
        workspace = _workspace_contents(root)
        if subscription.dropped:
            raise ScenarioMismatch(
                expected='L2 场景事件订阅不丢事件',
                received=f'EventBus subscription dropped={subscription.dropped}',
                next_action='缩短单轮事件突发，或为 ScenarioRunner 提供足够大的 EventBus 缓冲',
            )
        sequences = [event.sequence for event in events]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ScenarioMismatch(
                expected='事件 sequence 从 1 开始连续递增且不重复',
                received=f'收到 {sequences!r}',
                next_action='检查 EventBus 是否在广播前为一次 emit 统一分配一个新序号',
            )
        return ScenarioResult(
            name=scenario.name,
            final_output=MappingProxyType(final_output),
            tool_order=MappingProxyType(tool_order),
            stop_reasons=MappingProxyType(stop_reasons),
            errors=MappingProxyType(dict(errors)),
            statuses=MappingProxyType(statuses),
            events=tuple(events),
            workspace=MappingProxyType(workspace),
        )

    def _assert_result(self, expected: ScenarioExpectation, actual: ScenarioResult) -> None:
        pairs: tuple[tuple[str, object, object], ...] = (
            ('final_output', dict(expected.final_output), dict(actual.final_output)),
            ('tool_order', dict(expected.tool_order), dict(actual.tool_order)),
            ('stop_reasons', dict(expected.stop_reasons), dict(actual.stop_reasons)),
            ('errors', dict(expected.errors), dict(actual.errors)),
            ('statuses', dict(expected.statuses), dict(actual.statuses)),
            ('workspace', dict(expected.workspace), dict(actual.workspace)),
        )
        for field, wanted, got in pairs:
            if field in expected.specified and wanted != got:
                raise ScenarioMismatch(
                    expected=f'expect.{field} 与场景运行结果逐字相等：{wanted!r}',
                    received=f'{field} 实际为 {got!r}',
                    next_action='修复运行时行为，或在需求确实变化时更新 YAML 的 expect',
                )
        kinds = tuple(event.kind for event in actual.events)
        if 'events' in expected.specified and expected.events != kinds:
            raise ScenarioMismatch(
                expected=f'expect.events 与事件 kind 顺序逐项相等：{expected.events!r}',
                received=f'实际事件顺序为 {kinds!r}',
                next_action='修复事件顺序，或在需求确实变化时更新 YAML 的 expect.events',
            )
