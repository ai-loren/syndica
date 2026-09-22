"""Runtime-backed implementation of the command-line host protocol."""

from __future__ import annotations

import asyncio
import json
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

from syndica.cli.config import CliConfig, load_config
from syndica.cli.main import CliIO, InspectRequest, RunRequest
from syndica.kernel import (
    LLM,
    AgentCatalog,
    AgentFactory,
    AgentOptions,
    AgentSlot,
    BaseAgent,
    ConfigError,
    Context,
    ContractViolation,
    EventBus,
    ExitMode,
    FlagDeclaration,
    HooksCenter,
    HostFacts,
    HumanInteractionModule,
    LocalWorkspace,
    MailboxConfirmationHandler,
    Runtime,
    SafetyModule,
    StopReason,
    Subscription,
    Topology,
    TrustLevel,
    local_tools,
    register_builtins,
)

__all__ = ['RuntimeCliHost']

ModelFactory = Callable[[str], LLM]


def _default_config(path: Path) -> CliConfig:
    catalog = AgentCatalog()
    register_builtins(catalog)
    return CliConfig(source=str(path), catalog=catalog, topologies=MappingProxyType({}))


def _modules() -> tuple[SafetyModule, HumanInteractionModule]:
    return SafetyModule(), HumanInteractionModule()


def _model_names(config: CliConfig) -> tuple[str, ...]:
    return tuple(dict.fromkeys(config.catalog.get(name).model for name in config.catalog.names()))


def _last_output(context: Context) -> str:
    if context.stop_reason is StopReason.ERROR:
        raise ContractViolation(
            expected='CLI 请求对应的 Agent 以 completed / max_turns / aborted 之一结束',
            received='Context.stop_reason 是 error',
            next_action='查看当前命令之前输出的 Runtime 错误日志，修复失败的 LLM、工具或 hook',
        )
    outputs = [
        block.content
        for block in context.current_traj
        if block.role == 'assistant' and isinstance(block.content, str) and block.content
    ]
    return outputs[-1] if outputs else ''


def _with_max_turns(factory: AgentFactory, max_turns: int | None) -> AgentFactory:
    if max_turns is None:
        return factory

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = factory(hooks, events)
        existing = agent.options.max_turns
        tightened = max_turns if existing is None else min(existing, max_turns)
        agent.options = replace(agent.options, max_turns=tightened)
        return agent

    return build


def _config_error(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _request_flags(
    assignments: tuple[str, ...],
    declarations: tuple[FlagDeclaration, ...],
) -> dict[str, bool | int | str]:
    declared = {item.key: item for item in declarations}
    parsed: dict[str, bool | int | str] = {}
    for index, assignment in enumerate(assignments):
        key, separator, raw = assignment.partition('=')
        if not separator or not key:
            raise _config_error(
                '每个 --flag 使用非空 key=value',
                f'第 {index + 1} 项是 {assignment!r}',
                '改成 --flag key=value；用 flags list 查看可用 key',
            )
        declaration = declared.get(key)
        if declaration is None:
            raise _config_error(
                f'--flag key 是已声明项之一：{list(declared)!r}',
                f'收到未知 key {key!r}',
                '运行 syndica flags list，再逐字使用其中一个 key',
            )
        if key in parsed:
            raise _config_error(
                '每个 --flag key 只出现一次',
                f'key {key!r} 重复',
                '删除重复项，每个 key 只保留一个值',
            )
        default = declaration.default
        if type(default) is bool:
            if raw not in {'true', 'false'}:
                raise _config_error(
                    f'Flag {key!r} 是 bool，值只能是 true 或 false',
                    f'收到 {raw!r}',
                    f'改成 --flag {key}=true 或 --flag {key}=false',
                )
            value: bool | int | str = raw == 'true'
        elif type(default) is int:
            try:
                value = int(raw)
            except ValueError as exc:
                raise _config_error(
                    f'Flag {key!r} 是 int',
                    f'收到 {raw!r}',
                    f'改成 --flag {key}=<整数>',
                ) from exc
        else:
            value = raw
        if declaration.choices and value not in declaration.choices:
            raise _config_error(
                f'Flag {key!r} 是 {list(declaration.choices)!r} 之一',
                f'收到 {value!r}',
                f'改成 --flag {key}=<声明列出的值>',
            )
        if type(value) is int and declaration.minimum is not None and value < declaration.minimum:
            raise _config_error(
                f'Flag {key!r} 大于或等于 {declaration.minimum}',
                f'收到 {value}',
                f'提高 --flag {key}=... 的值',
            )
        if type(value) is int and declaration.maximum is not None and value > declaration.maximum:
            raise _config_error(
                f'Flag {key!r} 小于或等于 {declaration.maximum}',
                f'收到 {value}',
                f'降低 --flag {key}=... 的值',
            )
        parsed[key] = value
    return parsed


def _validated_request(request: RunRequest) -> dict[str, bool | int | str]:
    unsupported = [
        name
        for name, value in (
            ('max_cost', request.max_cost),
            ('max_tokens', request.max_tokens),
            ('max_wall_clock', request.max_wall_clock),
        )
        if value is not None
    ]
    if unsupported:
        raise _config_error(
            '里程碑 1 只执行 max_turns 预算',
            f'收到尚未实现的预算 {unsupported!r}',
            '删除这些参数；cost / token / wall-clock 预算执行器属于里程碑 8',
        )
    if request.agent is not None and request.topology is not None:
        raise _config_error(
            'run 只选择 --agent 或 --topology 之一',
            f'同时收到 agent={request.agent!r} topology={request.topology!r}',
            '删除其中一个目标参数后重试',
        )
    declarations = tuple(item for module in _modules() for item in module.flags)
    return _request_flags(request.flags, declarations)


def _with_root_turn_limit(
    topology: Topology,
    max_turns: int | None,
    catalog: AgentCatalog,
) -> Topology:
    if max_turns is None:
        return topology
    slots: list[AgentSlot] = []
    for slot in topology.slots:
        if slot.is_root:
            manifest = catalog.get(slot.agent)
            role_limit = (
                AgentOptions.max_turns if manifest.budget is None else manifest.budget['turns']
            )
            slot_limit = None if slot.budget is None else slot.budget['turns']
            if slot.budget is not None and (
                (slot_limit is None and role_limit is not None)
                or (slot_limit is not None and role_limit is not None and slot_limit > role_limit)
            ):
                slots.append(slot)
                continue
            current = role_limit if slot.budget is None else slot_limit
            tightened = max_turns if current is None else min(current, max_turns)
            slots.append(replace(slot, budget=MappingProxyType({'turns': tightened})))
        else:
            slots.append(slot)
    return Topology(slots=tuple(slots))


@dataclass
class _ChatSession:
    config: str
    runtime: Runtime
    workspace: LocalWorkspace
    agent_id: str
    subscription: Subscription
    pump: asyncio.Task[None]


class RuntimeCliHost:
    """Own Runtime and workspace lifecycles for synchronous console commands."""

    def __init__(
        self,
        *,
        io: CliIO,
        model_factory: ModelFactory,
        workspace_root: str | Path,
    ) -> None:
        self._io = io
        self._model_factory = model_factory
        self._workspace_root = Path(workspace_root)
        self._runner = asyncio.Runner()
        self._closed = False
        self._sessions: dict[str, _ChatSession] = {}

    def run_request(self, request: RunRequest) -> str:
        flags = _validated_request(request)
        if request.session is not None:
            overrides = [
                name
                for name, value in (
                    ('agent', request.agent),
                    ('topology', request.topology),
                    ('max_turns', request.max_turns),
                    ('flags', request.flags),
                )
                if value is not None and value != ()
            ]
            if overrides:
                raise _config_error(
                    '活动 session 沿用 spawn 时的 Agent、预算与 Flag',
                    f'这些参数会被忽略：{overrides!r}',
                    '删除这些参数；要改变装配请退出 chat，再创建一个新会话',
                )
            return self._runner.run(self._run_chat_request(request))
        return self._runner.run(self._run_request(request, flags))

    def inspect(self, request: InspectRequest) -> str:
        if request.resource == 'flags':
            declarations = tuple(item for module in _modules() for item in module.flags)
            if request.action == 'list':
                return '\n'.join(item.key for item in declarations)
            declaration = next(
                (item for item in declarations if item.key == request.name),
                None,
            )
            if declaration is None:
                raise _config_error(
                    f'Flag 是已声明项之一：{[item.key for item in declarations]!r}',
                    f'收到 {request.name!r}',
                    '运行 syndica flags list，再逐字使用其中一个 key',
                )
            return json.dumps(
                {
                    'key': declaration.key,
                    'default': declaration.default,
                    'description': declaration.description,
                    'choices': list(declaration.choices),
                    'minimum': declaration.minimum,
                    'maximum': declaration.maximum,
                },
                ensure_ascii=False,
                indent=2,
            )
        config = self._config(request.config)
        if request.resource == 'agents':
            if request.action == 'list':
                return '\n'.join(config.catalog.names())
            manifest = config.catalog.get(request.name)
            return json.dumps(
                {
                    'name': manifest.name,
                    'description': manifest.description,
                    'system_prompt': manifest.system_prompt,
                    'tools': list(manifest.tools),
                    'subagents': list(manifest.subagents),
                    'model': manifest.model,
                    'extends': manifest.extends,
                    'skills': list(manifest.skills),
                    'budget': None if manifest.budget is None else dict(manifest.budget),
                    'artifact_kinds': list(manifest.artifact_kinds),
                    'source': {
                        'kind': manifest.source.kind,
                        'location': manifest.source.location,
                        'origin': manifest.source.origin,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        if request.action == 'list':
            return '\n'.join(config.topologies)
        if request.name is None:
            raise _config_error(
                'topology show 带一个名字',
                '收到 name=None',
                '运行 syndica topology list，再把名字传给 topology show',
            )
        topology = config.topologies.get(request.name)
        if topology is None:
            raise _config_error(
                f'topology 是配置中的名字之一：{list(config.topologies)!r}',
                f'收到 {request.name!r}',
                '运行 syndica topology list，再逐字使用其中一个名字',
            )
        return json.dumps(
            {
                'name': request.name,
                'slots': [
                    {
                        'name': slot.name,
                        'agent': slot.agent,
                        'is_root': slot.is_root,
                        'cardinality': slot.cardinality,
                        'key_field': slot.key_field,
                        'boot': slot.boot,
                        'exit_mode': slot.exit_mode.value,
                        'extra_tools': list(slot.extra_tools),
                        'budget': None if slot.budget is None else dict(slot.budget),
                        'metadata': dict(slot.metadata),
                    }
                    for slot in topology.slots
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    def doctor(self, config: str) -> str:
        return self._runner.run(self._doctor(config))

    def open_chat(self, config: str) -> str:
        return self._runner.run(self._open_chat(config))

    def active_agents(self) -> str:
        return self._runner.run(self._active_agents())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._runner.run(self._close_sessions())
        self._runner.close()

    def _config(self, path: str) -> CliConfig:
        file = Path(path)
        if path == '.syndica.yaml' and not file.exists():
            return _default_config(file)
        return load_config(file)

    def _prompt(self, text: str) -> None:
        self._io.stdout.write(text)
        self._io.stdout.flush()

    async def _event_pump(self, runtime: Runtime, subscription: Subscription) -> None:
        async for event in subscription:
            if event.kind == 'user_input_requested':
                self._prompt(f'{event.payload["question"]}\n> ')
                eof_action = 'abort'
            elif event.kind == 'user_question_requested':
                options = cast(list[object], event.payload['options'])
                choices = '\n'.join(
                    f'  {index}. {option}' for index, option in enumerate(options, start=1)
                )
                self._prompt(f'{event.payload["question"]}\n{choices}\n> ')
                eof_action = 'abort'
            elif event.kind == 'tool_confirmation_requested':
                self._prompt(
                    f'Approve {event.payload["tool_name"]} '
                    f'({event.payload["tool_call_id"]}) with '
                    f'{event.payload["arguments"]}? [approve/reject]\n> '
                )
                eof_action = 'reject'
            else:
                continue
            answer = await asyncio.to_thread(self._io.stdin.readline)
            if answer == '' and eof_action == 'abort':
                await runtime.abort(event.agent_id)
                continue
            delivered = await runtime.send(
                event.agent_id,
                'reject' if answer == '' else answer.rstrip('\r\n'),
                activate=False,
                sender='human',
                correlation_id=cast(str, event.payload['request_id']),
                trust=TrustLevel.TRUSTED,
            )
            if not delivered.ok:
                raise RuntimeError(delivered.reason or 'human response delivery failed')

    async def _doctor(self, path: str) -> str:
        config = self._config(path)
        workspace = LocalWorkspace(
            str(self._workspace_root),
            shell='/bin/sh',
            env={},
        )
        try:
            tools = local_tools(workspace.portal())
            names = _model_names(config)
            models: Mapping[str, LLM] = {name: self._model_factory(name) for name in names}
            config.catalog.freeze(
                tools,
                models,
                HostFacts(
                    os=platform.system().lower(),
                    platform=platform.platform(),
                    shell='/bin/sh',
                ),
            )
        finally:
            await workspace.close()
        return (
            f'config: ok {config.source}\n'
            f'models: ok {", ".join(names)}\n'
            f'workspace: ok {self._workspace_root}'
        )

    async def _open_chat(self, path: str) -> str:
        config = self._config(path)
        workspace = LocalWorkspace(
            str(self._workspace_root),
            shell='/bin/sh',
            env={},
        )
        runtime: Runtime | None = None
        subscription: Subscription | None = None
        pump: asyncio.Task[None] | None = None
        try:
            tools = local_tools(workspace.portal())
            models: Mapping[str, LLM] = {
                name: self._model_factory(name) for name in _model_names(config)
            }
            factories = config.catalog.freeze(
                tools,
                models,
                HostFacts(
                    os=platform.system().lower(),
                    platform=platform.platform(),
                    shell='/bin/sh',
                ),
            )
            runtime = Runtime(
                agents=factories,
                orchestration=True,
                modules=_modules(),
                confirmation_handler=MailboxConfirmationHandler(timeout=300),
            )
            subscription = runtime.events.subscribe()
            pump = asyncio.create_task(
                self._event_pump(runtime, subscription),
                name='cli-chat-human-events',
            )
            factory = runtime.agent_factory('Orchestrator')
            if factory is None:
                config.catalog.get('Orchestrator')
                raise AssertionError('catalog.get returned without an Orchestrator runtime factory')
            agent_id = await runtime.spawn(factory, exit_mode=ExitMode.ZOMBIE)
            self._sessions[agent_id] = _ChatSession(
                config=path,
                runtime=runtime,
                workspace=workspace,
                agent_id=agent_id,
                subscription=subscription,
                pump=pump,
            )
            return agent_id
        except BaseException:
            if runtime is not None:
                await runtime.shutdown()
            if pump is not None:
                await pump
            if subscription is not None:
                subscription.close()
            await workspace.close()
            raise

    async def _run_session(self, session: _ChatSession, prompt: str) -> str:
        delivered = await session.runtime.send(session.agent_id, prompt)
        if not delivered.ok:
            raise _config_error(
                f'chat target {session.agent_id!r} 接受新消息',
                delivered.reason or 'broker 未给出失败原因',
                '输入 :agents 检查目标状态，或退出后重新进入 chat',
            )
        context = await self._wait_with_pump(
            session.runtime,
            session.agent_id,
            session.pump,
        )
        return _last_output(context)

    async def _run_chat_request(self, request: RunRequest) -> str:
        target = request.session
        if target is None:
            raise AssertionError('session branch entered without a target')
        seen: set[int] = set()
        for root_session in self._sessions.values():
            identity = id(root_session.runtime)
            if identity in seen:
                continue
            seen.add(identity)
            if await root_session.runtime.find(target) is None:
                continue
            session = replace(root_session, agent_id=target)
            if request.config != session.config:
                raise _config_error(
                    '同一 chat session 沿用创建时的 config',
                    f'创建时是 {session.config!r}，本次收到 {request.config!r}',
                    '不要在 chat 中切换 --config；退出后用新配置重新进入 chat',
                )
            return await self._run_session(session, request.prompt)
        raise _config_error(
            'run --session 指向当前 chat 进程里的活动 Agent',
            (f'收到 session={target!r}；当前进程没有该目标，且里程碑 1 没有 SessionStore'),
            '在 chat 中先输入 :agents 查看当前目标；跨进程恢复留到里程碑 6',
        )

    async def _active_agents(self) -> str:
        rows: list[str] = []
        seen: set[int] = set()
        for session in self._sessions.values():
            identity = id(session.runtime)
            if identity in seen:
                continue
            seen.add(identity)
            for info in await session.runtime.registry.list():
                rows.append(f'{info.agent_id} {info.name} {info.status.value}')
        return '\n'.join(rows)

    async def _close_sessions(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await session.runtime.shutdown()
            await session.pump
            session.subscription.close()
            await session.workspace.close()

    async def _wait_with_pump(
        self,
        runtime: Runtime,
        agent_id: str,
        pump: asyncio.Task[None],
    ) -> Context:
        wait = asyncio.create_task(runtime.wait(agent_id), name=f'cli-wait:{agent_id}')
        done, _ = await asyncio.wait({wait, pump}, return_when=asyncio.FIRST_COMPLETED)
        if pump in done:
            failure = pump.exception()
            if failure is not None:
                await runtime.abort(agent_id)
                await asyncio.gather(wait, return_exceptions=True)
                raise failure
        return await wait

    async def _run_request(
        self,
        request: RunRequest,
        flags: Mapping[str, bool | int | str],
    ) -> str:
        config = self._config(request.config)
        workspace = LocalWorkspace(
            str(self._workspace_root),
            shell='/bin/sh',
            env={},
        )
        tools = local_tools(workspace.portal())
        try:
            model_names = _model_names(config)
            models: Mapping[str, LLM] = {name: self._model_factory(name) for name in model_names}
            facts = HostFacts(
                os=platform.system().lower(),
                platform=platform.platform(),
                shell='/bin/sh',
            )
            factories = config.catalog.freeze(tools, models, facts)
            runtime = Runtime(
                agents={} if request.topology is not None else factories,
                orchestration=True,
                modules=_modules(),
                flags=flags,
                confirmation_handler=MailboxConfirmationHandler(timeout=300),
            )
            subscription = runtime.events.subscribe()
            pump = asyncio.create_task(
                self._event_pump(runtime, subscription),
                name='cli-human-events',
            )
            try:
                if request.topology is not None:
                    topology = config.topologies.get(request.topology)
                    if topology is None:
                        raise _config_error(
                            f'--topology 是配置中的名字之一：{list(config.topologies)!r}',
                            f'收到 {request.topology!r}',
                            '运行 syndica topology list，再逐字使用其中一个名字',
                        )
                    roots = [slot for slot in topology.slots if slot.is_root]
                    if not roots:
                        raise _config_error(
                            f'topology {request.topology!r} 有一个 root 槽',
                            '所有 topology.slots 的 is_root 都是 false',
                            '给要接收 CLI prompt 的 singleton 槽设置 is_root: true',
                        )
                    root_slot = roots[0]
                    topology = _with_root_turn_limit(
                        topology,
                        request.max_turns,
                        config.catalog,
                    )
                    await runtime.assemble(
                        topology,
                        config.catalog,
                        tools=tools,
                        models=models,
                        host=facts,
                    )
                    delivered = await runtime.send_to_slot(root_slot.name, request.prompt)
                    if not delivered.ok:
                        raise _config_error(
                            f'拓扑根槽 {root_slot.name!r} 接受输入',
                            delivered.reason or 'broker 未给出失败原因',
                            '检查根槽 boot/cardinality 与 Runtime 状态',
                        )
                    agent_id = await runtime.resolve(root_slot.name)
                    if agent_id is None:
                        raise AssertionError('root slot accepted input without an agent id')
                else:
                    role = request.agent or 'Orchestrator'
                    factory = runtime.agent_factory(role)
                    if factory is None:
                        config.catalog.get(role)
                        raise AssertionError(
                            'catalog.get returned without a matching runtime factory'
                        )
                    agent_id = await runtime.spawn(
                        _with_max_turns(factory, request.max_turns),
                        query=request.prompt,
                        exit_mode=ExitMode.ZOMBIE,
                    )
                context = await self._wait_with_pump(runtime, agent_id, pump)
                return _last_output(context)
            finally:
                await runtime.shutdown()
                await pump
                subscription.close()
        finally:
            await workspace.close()
