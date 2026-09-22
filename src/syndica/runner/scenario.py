"""Declarative L2 scenario replay over the real Syndica kernel."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from syndica.kernel import (
    AgentCatalog,
    AgentFactory,
    AgentSlot,
    AgentStatus,
    BaseAgent,
    ConfigError,
    ContextOverflowError,
    EventBus,
    ExitMode,
    HooksCenter,
    HostFacts,
    HumanInteractionModule,
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
from syndica.runner.yaml import load_yaml_mapping

__all__ = [
    'Scenario',
    'ScenarioAgent',
    'ScenarioEvent',
    'ScenarioExpectation',
    'ScenarioMismatch',
    'ScenarioResponse',
    'ScenarioResult',
    'ScenarioRunner',
    'load_scenario',
]

_ROOT_KEYS = frozenset({'version', 'name', 'agents', 'topology', 'entry', 'responses', 'expect'})
_AGENT_KEYS = frozenset({'manifest', 'instances'})
_INSTANCE_KEYS = frozenset({'turns'})
_TURN_KEYS = frozenset({'content', 'tool_calls', 'raises'})
_CALL_KEYS = frozenset({'id', 'name', 'arguments'})
_ERROR_KEYS = frozenset({'kind', 'message'})
_ERROR_KINDS = frozenset({'runtime_error', 'context_overflow', 'llm_response_error'})
_TOPOLOGY_KEYS = frozenset({'slots'})
_SLOT_KEYS = frozenset(
    {
        'name',
        'agent',
        'is_root',
        'cardinality',
        'key_field',
        'boot',
        'exit_mode',
        'extra_tools',
        'budget',
        'metadata',
    }
)
_ENTRY_KEYS = frozenset({'slot', 'prompt'})
_RESPONSE_KEYS = frozenset({'kind', 'agent', 'answer', 'tool_call_id'})
_RESPONSE_KINDS = frozenset({'user_input', 'user_question', 'confirmation'})
_EXPECT_KEYS = frozenset(
    {'final_output', 'tool_order', 'stop_reasons', 'statuses', 'events', 'workspace'}
)


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
class ScenarioExpectation:
    final_output: Mapping[str, str]
    tool_order: Mapping[str, tuple[str, ...]]
    stop_reasons: Mapping[str, str]
    statuses: Mapping[str, str]
    events: tuple[str, ...]
    workspace: Mapping[str, str]
    specified: frozenset[str]


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
    statuses: Mapping[str, str]
    events: tuple[ScenarioEvent, ...]
    workspace: Mapping[str, str]


def _error(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _mapping(
    value: object,
    field: str,
    *,
    allowed: frozenset[str] | None = None,
    required: frozenset[str] = frozenset(),
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(
            f'{field} 是 Mapping',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            f'把 {field} 写成 YAML 对象',
        )
    raw = cast(Mapping[object, object], value)
    bad_keys = [key for key in raw if not isinstance(key, str)]
    if bad_keys:
        raise _error(
            f'{field} 的键都是字符串',
            f'非字符串键为 {bad_keys!r}',
            '把这些键改成场景格式声明的字段名',
        )
    typed = cast(Mapping[str, object], raw)
    if allowed is not None:
        unknown = sorted(key for key in typed if key not in allowed)
        if unknown:
            raise _error(
                f'{field} 只含 {sorted(allowed)!r}',
                f'未知键 {unknown!r}',
                '删除未知键；场景字段写错时不能静默忽略',
            )
    missing = sorted(required - set(typed))
    if missing:
        raise _error(
            f'{field} 含必填键 {sorted(required)!r}',
            f'缺少 {missing!r}',
            '补齐缺失字段后重试',
        )
    return typed


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise _error(
            f'{field} 是 YAML 列表',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            f'把 {field} 写成 `- ...` 列表',
        )
    return cast(Sequence[object], value)


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(
            f'{field} 是非空白字符串',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            f'给 {field} 填写可逐字匹配的字符串',
        )
    return value


def load_scenario(path: object) -> Scenario:
    """Parse one v1 YAML scenario without importing any backend."""
    root, file = load_yaml_mapping(path)
    root = _mapping(
        root,
        'scenario',
        allowed=_ROOT_KEYS,
        required=_ROOT_KEYS,
    )
    version = root['version']
    if type(version) is not int or version != 1:
        raise _error(
            'scenario.version 是整数 1',
            f'收到 version={version!r}，类型是 {type(version).__name__}',
            '写成 `version: 1`；不要写 true、字符串或其他版本',
        )
    name = _nonblank(root['name'], 'scenario.name')
    catalog = AgentCatalog()
    register_builtins(catalog)
    agents = _agents(root['agents'], catalog, file)
    topology = _topology(root['topology'])
    topology.validate(catalog)
    missing_scripts = sorted({slot.agent for slot in topology.slots} - set(agents))
    if missing_scripts:
        raise _error(
            'topology 引用的每个角色都在 scenario.agents 中提供 instances 脚本',
            f'缺少脚本的角色为 {missing_scripts!r}',
            '给每个角色添加 manifest 与至少一个 instances[].turns',
        )
    entry = _mapping(
        root['entry'],
        'entry',
        allowed=_ENTRY_KEYS,
        required=_ENTRY_KEYS,
    )
    entry_slot = _nonblank(entry['slot'], 'entry.slot')
    if entry_slot not in {slot.name for slot in topology.slots}:
        raise _error(
            'entry.slot 是 topology.slots 中的槽名',
            f'收到 {entry_slot!r}；现有槽为 {[slot.name for slot in topology.slots]!r}',
            '把 entry.slot 改成现有槽名',
        )
    prompt = _nonblank(entry['prompt'], 'entry.prompt')
    responses = _responses(root['responses'])
    return Scenario(
        version=1,
        name=name,
        catalog=catalog,
        agents=MappingProxyType(agents),
        topology=topology,
        entry_slot=entry_slot,
        prompt=prompt,
        responses=responses,
        expect=_expectation(root['expect']),
        source=str(file),
    )


def _agents(
    value: object,
    catalog: AgentCatalog,
    file: Path,
) -> dict[str, ScenarioAgent]:
    mapping = _mapping(value, 'agents')
    if not mapping:
        raise _error(
            'agents 至少声明一个角色',
            '收到空 Mapping',
            '添加根角色及其 manifest / instances',
        )
    agents: dict[str, ScenarioAgent] = {}
    all_call_ids: set[str] = set()
    for role, raw_agent in mapping.items():
        role = _nonblank(role, 'agents 的角色名')
        data = _mapping(
            raw_agent,
            f'agents.{role}',
            allowed=_AGENT_KEYS,
            required=_AGENT_KEYS,
        )
        manifest_data = _mapping(data['manifest'], f'agents.{role}.manifest')
        if manifest_data.get('impl') is not None:
            raise _error(
                'L2 场景 Agent 使用声明式 manifest，不加载任意 Python 类',
                f'agents.{role}.manifest.impl={manifest_data.get("impl")!r}',
                '删除 impl；需要的行为用真实工具与 ScriptedLLM turns 表达',
            )
        manifest = catalog.load_mapping(
            manifest_data,
            source=ManifestSource(
                kind='yaml_file',
                location=f'{str(file)}#agents.{role}.manifest',
                origin=str(file.parent.resolve()),
            ),
        )
        if manifest.name != role:
            raise _error(
                f'agents 键与 manifest.name 都是 {role!r}',
                f'manifest.name={manifest.name!r}',
                '让两处名字逐字相同；稳定 Agent selector 使用外层角色名',
            )
        raw_instances = _sequence(data['instances'], f'agents.{role}.instances')
        if not raw_instances:
            raise _error(
                f'agents.{role}.instances 至少有一份脚本',
                '收到空列表',
                '添加 `{turns: [...]}`；每个运行时实例消费一份独立脚本',
            )
        instances = tuple(
            _instance(role, index, item, all_call_ids) for index, item in enumerate(raw_instances)
        )
        agents[role] = ScenarioAgent(manifest=manifest, instances=instances)
    return agents


def _instance(
    role: str,
    index: int,
    value: object,
    all_call_ids: set[str],
) -> tuple[ScriptedTurn, ...]:
    field = f'agents.{role}.instances[{index}]'
    data = _mapping(
        value,
        field,
        allowed=_INSTANCE_KEYS,
        required=_INSTANCE_KEYS,
    )
    return tuple(
        _turn(role, index, turn_index, item, all_call_ids)
        for turn_index, item in enumerate(_sequence(data['turns'], f'{field}.turns'))
    )


def _turn(
    role: str,
    instance: int,
    index: int,
    value: object,
    all_call_ids: set[str],
) -> ScriptedTurn:
    field = f'agents.{role}.instances[{instance}].turns[{index}]'
    data = _mapping(value, field, allowed=_TURN_KEYS)
    calls = [
        _tool_call(field, call_index, item, all_call_ids)
        for call_index, item in enumerate(
            _sequence(data.get('tool_calls', ()), f'{field}.tool_calls')
        )
    ]
    content = data.get('content')
    if content is not None and not isinstance(content, str):
        raise _error(
            f'{field}.content 是字符串或 null',
            f'收到 {content!r}，类型是 {type(content).__name__}',
            '改成模型会返回的文本，或省略',
        )
    raised = _raised(data['raises'], field) if 'raises' in data else None
    return ScriptedTurn(content=content, tool_calls=calls, raises=raised)


def _tool_call(
    turn_field: str,
    index: int,
    value: object,
    all_call_ids: set[str],
) -> ToolCall:
    field = f'{turn_field}.tool_calls[{index}]'
    data = _mapping(
        value,
        field,
        allowed=_CALL_KEYS,
        required=_CALL_KEYS,
    )
    call_id = _nonblank(data['id'], f'{field}.id')
    if call_id in all_call_ids:
        raise _error(
            '一个场景内每个 tool call id 只出现一次',
            f'{field}.id={call_id!r} 重复',
            '给每次调用一个稳定且唯一的 id；重复会让结果配对与确认答复指向错误调用',
        )
    all_call_ids.add(call_id)
    name = _nonblank(data['name'], f'{field}.name')
    arguments = _mapping(data['arguments'], f'{field}.arguments')
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise _error(
            f'{field}.arguments 只含 JSON 值',
            f'编码失败：{type(exc).__name__}: {exc}',
            '把值改成字符串、数字、布尔值、null、列表或 Mapping',
        ) from exc
    return ToolCall(id=call_id, name=name, arguments=encoded)


def _raised(value: object, field: str) -> BaseException:
    data = _mapping(
        value,
        f'{field}.raises',
        allowed=_ERROR_KEYS,
        required=_ERROR_KEYS,
    )
    kind = _nonblank(data['kind'], f'{field}.raises.kind')
    message = _nonblank(data['message'], f'{field}.raises.message')
    if kind == 'runtime_error':
        return RuntimeError(message)
    if kind == 'context_overflow':
        return ContextOverflowError(
            expected='场景脚本指定一次 context overflow',
            received=message,
            next_action='由被测 overflow hook 处理，或把 expect.stop_reasons 设为 error',
        )
    if kind == 'llm_response_error':
        return LLMResponseError(
            expected='场景脚本指定一次 LLM response error',
            received=message,
            next_action='由被测 llm_failure hook 处理，或把 expect.stop_reasons 设为 error',
        )
    raise _error(
        f'{field}.raises.kind 是 {sorted(_ERROR_KINDS)!r} 之一',
        f'收到 {kind!r}',
        '改成 runtime_error / context_overflow / llm_response_error；不接受 import path',
    )


def _topology(value: object) -> Topology:
    data = _mapping(
        value,
        'topology',
        allowed=_TOPOLOGY_KEYS,
        required=_TOPOLOGY_KEYS,
    )
    slots: list[AgentSlot] = []
    for index, raw_slot in enumerate(_sequence(data['slots'], 'topology.slots')):
        field = f'topology.slots[{index}]'
        slot = dict(_mapping(raw_slot, field, allowed=_SLOT_KEYS))
        raw_exit = slot.get('exit_mode')
        if raw_exit is not None:
            try:
                slot['exit_mode'] = ExitMode(raw_exit)
            except (TypeError, ValueError) as exc:
                raise _error(
                    f'{field}.exit_mode 是 idle / zombie / exit 之一',
                    f'收到 {raw_exit!r}',
                    '改成小写字符串 idle、zombie 或 exit',
                ) from exc
        try:
            parsed = AgentSlot(**cast(Any, slot))
        except TypeError as exc:
            raise _error(
                f'{field} 至少含 name 与 agent',
                f'构造失败：{exc}',
                '补齐必填字段并删除未知字段',
            ) from exc
        if (
            parsed.cardinality != 'singleton'
            or parsed.exit_mode is not ExitMode.ZOMBIE
            or parsed.extra_tools
            or parsed.budget is not None
        ):
            raise _error(
                'L2 v1 的 slot 使用 singleton + zombie，且不带 extra_tools / budget',
                (
                    f'{field} 为 cardinality={parsed.cardinality!r}、'
                    f'exit_mode={parsed.exit_mode.value!r}、'
                    f'extra_tools={parsed.extra_tools!r}、budget={parsed.budget!r}'
                ),
                (
                    '把场景槽收窄到最小回放形态；完整 topology 装配已有独立集成测试，'
                    'L2 v1 不静默忽略实例键或槽级增强'
                ),
            )
        slots.append(parsed)
    return Topology(slots=tuple(slots))


def _responses(value: object) -> tuple[ScenarioResponse, ...]:
    responses: list[ScenarioResponse] = []
    for index, raw in enumerate(_sequence(value, 'responses')):
        field = f'responses[{index}]'
        data = _mapping(
            raw,
            field,
            allowed=_RESPONSE_KEYS,
            required=frozenset({'kind', 'agent', 'answer'}),
        )
        kind = _nonblank(data['kind'], f'{field}.kind')
        if kind not in _RESPONSE_KINDS:
            raise _error(
                f'{field}.kind 是 {sorted(_RESPONSE_KINDS)!r} 之一',
                f'收到 {kind!r}',
                '改成 user_input / user_question / confirmation',
            )
        tool_call_id = data.get('tool_call_id')
        if tool_call_id is not None:
            tool_call_id = _nonblank(tool_call_id, f'{field}.tool_call_id')
        if kind != 'confirmation' and tool_call_id is not None:
            raise _error(
                f'{field}.tool_call_id 只用于 confirmation',
                f'kind={kind!r} 同时给了 tool_call_id={tool_call_id!r}',
                '删除 tool_call_id；普通人类问题按请求顺序匹配',
            )
        if kind == 'confirmation' and tool_call_id is None:
            raise _error(
                f'{field}.tool_call_id 是 confirmation 的必填原始工具调用 ID',
                f'kind={kind!r} 但没有 tool_call_id',
                '填写触发本次确认的 scripted tool_call_id；动态 request_id 不写进 YAML',
            )
        responses.append(
            ScenarioResponse(
                kind=kind,
                agent=_nonblank(data['agent'], f'{field}.agent'),
                answer=_nonblank(data['answer'], f'{field}.answer'),
                tool_call_id=tool_call_id,
            )
        )
    return tuple(responses)


def _string_map(value: object, field: str) -> Mapping[str, str]:
    mapping = _mapping(value, field)
    bad = [(key, item) for key, item in mapping.items() if not isinstance(item, str)]
    if bad:
        raise _error(
            f'{field} 的值都是字符串',
            f'不合格项为 {bad!r}',
            '把期望值写成逐字比较的字符串',
        )
    return MappingProxyType(dict(cast(Mapping[str, str], mapping)))


def _tool_order(value: object) -> Mapping[str, tuple[str, ...]]:
    mapping = _mapping(value, 'expect.tool_order')
    result: dict[str, tuple[str, ...]] = {}
    for agent, raw_names in mapping.items():
        names = _sequence(raw_names, f'expect.tool_order.{agent}')
        result[agent] = tuple(
            _nonblank(name, f'expect.tool_order.{agent}[{index}]')
            for index, name in enumerate(names)
        )
    return MappingProxyType(result)


def _expectation(value: object) -> ScenarioExpectation:
    data = _mapping(value, 'expect', allowed=_EXPECT_KEYS)
    events = tuple(
        _nonblank(item, f'expect.events[{index}]')
        for index, item in enumerate(_sequence(data.get('events', ()), 'expect.events'))
    )
    return ScenarioExpectation(
        final_output=_string_map(data.get('final_output', {}), 'expect.final_output'),
        tool_order=_tool_order(data.get('tool_order', {})),
        stop_reasons=_string_map(data.get('stop_reasons', {}), 'expect.stop_reasons'),
        statuses=_string_map(data.get('statuses', {}), 'expect.statuses'),
        events=events,
        workspace=_string_map(data.get('workspace', {}), 'expect.workspace'),
        specified=frozenset(data),
    )


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
        counters = {role: 0 for role in roles}
        factories = {
            role: self._factory(
                role,
                agent,
                base_factories[role],
                policy,
                counters,
                selector_by_id,
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
            return agent

        return build

    async def _result(
        self,
        scenario: Scenario,
        runtime: Runtime,
        selector_by_id: Mapping[str, str],
        events: list[ScenarioEvent],
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
            statuses=MappingProxyType(statuses),
            events=tuple(events),
            workspace=MappingProxyType(workspace),
        )

    def _assert_result(self, expected: ScenarioExpectation, actual: ScenarioResult) -> None:
        pairs: tuple[tuple[str, object, object], ...] = (
            ('final_output', dict(expected.final_output), dict(actual.final_output)),
            ('tool_order', dict(expected.tool_order), dict(actual.tool_order)),
            ('stop_reasons', dict(expected.stop_reasons), dict(actual.stop_reasons)),
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
