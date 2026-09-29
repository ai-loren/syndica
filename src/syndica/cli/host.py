"""Runtime-backed implementation of the command-line host protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from textwrap import indent
from types import MappingProxyType
from typing import cast

from syndica.cli.config import CliConfig, load_config
from syndica.cli.main import CliIO, InspectRequest, RunRequest
from syndica.kernel import (
    LLM,
    AgentCatalog,
    AgentOptions,
    AgentSlot,
    ConfigError,
    Context,
    ContractViolation,
    ExitMode,
    FlagDeclaration,
    HostFacts,
    HumanInteractionModule,
    LocalWorkspace,
    MailboxConfirmationHandler,
    Runtime,
    SafetyModule,
    StopReason,
    Subscription,
    SyndicaError,
    Topology,
    TrustLevel,
    local_tools,
    register_builtins,
)

__all__ = ['RuntimeCliHost']

ModelFactory = Callable[[str], LLM]
_RUNTIME_LOGGER = 'syndica.kernel.runtime'


class _RuntimeErrorCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _CaptureRuntimeErrors:
    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self._handler = _RuntimeErrorCapture()
        self._logger = logging.getLogger(_RUNTIME_LOGGER)
        self._previous_propagate = self._logger.propagate

    def __enter__(self) -> list[logging.LogRecord]:
        self._logger.addHandler(self._handler)
        self._logger.propagate = False
        self.records = self._handler.records
        return self.records

    def __exit__(self, *exc_info: object) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.propagate = self._previous_propagate


def _default_config(path: Path) -> CliConfig:
    catalog = AgentCatalog()
    register_builtins(catalog)
    return CliConfig(source=str(path), catalog=catalog, topologies=MappingProxyType({}))


def _modules() -> tuple[SafetyModule, HumanInteractionModule]:
    return SafetyModule(), HumanInteractionModule()


def _model_names(config: CliConfig) -> tuple[str, ...]:
    return tuple(dict.fromkeys(config.catalog.get(name).model for name in config.catalog.names()))


def _role_turns(catalog: AgentCatalog, name: str) -> int | None:
    manifest = catalog.get(name)
    return AgentOptions.max_turns if manifest.budget is None else manifest.budget['turns']


def _turn_capped(catalog: AgentCatalog, max_turns: int | None) -> AgentCatalog:
    """同一份目录，但每个角色的轮数预算都不超过 max_turns。

    委派与分叉出来的子 Agent 都由目录里的工厂造出（拓扑的 assemble 也用它覆盖同名工厂），
    所以上限必须落在角色上：只收紧根 Agent 时，一次委派会按角色预算（缺省 50 轮）跑满。
    """
    if max_turns is None:
        return catalog
    capped = AgentCatalog()
    for name in catalog.names():
        manifest = catalog.get(name)
        role_limit = _role_turns(catalog, name)
        data: dict[str, object] = {
            field.name: getattr(manifest, field.name)
            for field in fields(manifest)
            if field.name != 'source'
        }
        data['budget'] = {'turns': max_turns if role_limit is None else min(role_limit, max_turns)}
        # system_prompt 已是解析后的正文；留着 origin 会让它被当成相对路径再解析一次。
        capped.load_mapping(data, source=replace(manifest.source, origin=None))
    return capped


def _record_detail(record: logging.LogRecord) -> str:
    if record.exc_info is not None:
        error = record.exc_info[1]
        if isinstance(error, SyndicaError):
            return error.message
        if error is not None:
            return f'{type(error).__name__}: {error}'
    return record.getMessage()


def _runtime_error_report(
    record: logging.LogRecord, names: Mapping[str, str], position: str
) -> str:
    args = record.args if isinstance(record.args, tuple) else ()
    agent = next((arg for arg in args if isinstance(arg, str) and arg in names), None)
    where = '' if agent is None else f'（agent {names[agent]} {agent}）'
    return f'Runtime 错误 {position}{where}:\n{indent(_record_detail(record), "  ")}'


def _error_failure(records: list[logging.LogRecord]) -> ContractViolation:
    received = 'Context.stop_reason 是 error'
    next_action = '查看当前命令之前输出的 Runtime 错误日志，修复失败的 LLM、工具或 hook'
    if records:
        received = f'{received}；Runtime 最近错误：{_record_detail(records[-1])}'
        next_action = '先处理 Runtime 最近错误；如果它来自外部模型服务，修复配置、额度或稍后重试'
    return ContractViolation(
        expected='CLI 请求对应的 Agent 以 completed / max_turns / aborted 之一结束',
        received=received,
        next_action=next_action,
    )


# 按 StopReason 取全，外加被硬终止时的 None：缺一个键，那种结局就在 CLI 里撞成 KeyError。
_NO_ANSWER: dict[StopReason | None, tuple[str, str]] = {
    StopReason.COMPLETED: (
        '它正常结束，但没有留下任何文字',
        '重试一次；反复出现就换一个模型',
    ),
    StopReason.MAX_TURNS: (
        '它用完了轮次预算（max_turns），最后一轮还在调用工具',
        (
            '提高 --max-turns 后重试，或把任务拆小；'
            'chat 里同一个 Agent 的轮数跨消息累计，用完要重开 chat'
        ),
    ),
    StopReason.ABORTED: (
        '它被中止了，例如等人回答时 stdin 已经关闭',
        '在交互终端里重跑并回答它的提问，或在 prompt 里一次给全它需要的信息',
    ),
    StopReason.ERROR: (
        '它以错误结束',
        '先处理上面列出的 Runtime 错误，再重试',
    ),
    None: (
        '它在给出回答之前被硬终止了（kill / 关机）',
        '重试；被外部终止的 Agent 不会留下回答',
    ),
}

_CUT_SHORT: dict[StopReason | None, str] = {
    StopReason.MAX_TURNS: (
        '注意: Agent 用完了轮次预算（max_turns）才停下，本次输出是它最后留下的一段文字，'
        '可能不完整；需要完整结果就提高 --max-turns 重试'
    ),
    StopReason.ABORTED: '注意: Agent 被中止了，本次输出是它最后留下的一段文字，可能不完整',
}


def _no_answer(reason: StopReason | None) -> ContractViolation:
    why, next_action = _NO_ANSWER[reason]
    ended = None if reason is None else reason.value
    return ContractViolation(
        expected='CLI 请求对应的 Agent 用一段文字回答这次请求',
        received=f'Agent 结束时 stop_reason={ended}，没有留下文字回答：{why}',
        next_action=next_action,
    )


def _final_text(context: Context, start: int) -> str | None:
    for block in reversed(context.current_traj[start:]):
        if block.role == 'assistant' and isinstance(block.content, str) and block.content:
            return block.content
    return None


def _after_message(context: Context, message_id: str | None) -> int:
    """这条消息注入之后的位置；它没被注入（Agent 没读到它）时是轨迹末尾。

    chat 里同一个 Agent 的轨迹跨消息累积，不从这里往后找，就会把上一条消息的回答当成这一条的。
    没有回执 id 时不找：初始 query 那一块同样没有 message_id，拿 None 去比会把它当成这条消息。
    """
    if message_id is not None:
        for index, block in enumerate(context.current_traj):
            if block.role == 'user' and block.extra.get('message_id') == message_id:
                return index + 1
    return len(context.current_traj)


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


def _with_turn_limit(
    topology: Topology,
    max_turns: int | None,
    catalog: AgentCatalog,
) -> Topology:
    """每个槽都收紧到 max_turns。

    `catalog` 必须是收紧之前的目录：角色预算收紧之后，槽上原本合法的更大预算会被 assemble 当成
    放宽而拒绝；原本就在放宽的槽保持原样，让 assemble 照常报它。
    """
    if max_turns is None:
        return topology
    slots: list[AgentSlot] = []
    for slot in topology.slots:
        role_limit = _role_turns(catalog, slot.agent)
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

    def open_chat(self, config: str, max_turns: int | None = None) -> str:
        return self._runner.run(self._open_chat(config, max_turns))

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

    def _warn(self, text: str) -> None:
        self._io.stderr.write(f'{text}\n')
        self._io.stderr.flush()

    async def _answer(
        self,
        runtime: Runtime,
        context: Context,
        records: list[logging.LogRecord],
        *,
        start: int,
        fail_without_text: bool,
    ) -> str:
        """把目标 Agent 的终局交给用户：Runtime 错误写进 stderr，回答作为返回值。

        这些错误记录被 `_CaptureRuntimeErrors` 截下、不会冒到 logging；子 Agent 失败时根 Agent 照样
        可能给出回答，不在这里写出去，失败就只剩一个看起来正常的结论。被最终错误引用的那一条不重复写。
        """
        names = {info.agent_id: info.name for info in await runtime.registry.list()}
        failed = context.stop_reason is StopReason.ERROR
        listed = records[:-1] if failed else records
        for position, record in enumerate(listed, start=1):
            self._warn(_runtime_error_report(record, names, f'{position}/{len(listed)}'))
        if failed:
            raise _error_failure(records)
        text = _final_text(context, start)
        if text is None:
            missing = _no_answer(context.stop_reason)
            if fail_without_text:
                raise missing
            self._warn(missing.message)
            return ''
        notice = _CUT_SHORT.get(context.stop_reason)
        if notice is not None:
            self._warn(notice)
        return text

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

    async def _open_chat(self, path: str, max_turns: int | None) -> str:
        config = self._config(path)
        catalog = _turn_capped(config.catalog, max_turns)
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
            factories = catalog.freeze(
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
                catalog.get('Orchestrator')
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
        with _CaptureRuntimeErrors() as runtime_errors:
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
            return await self._answer(
                session.runtime,
                context,
                runtime_errors,
                start=_after_message(context, delivered.message_id),
                fail_without_text=False,
            )

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
        catalog = _turn_capped(config.catalog, request.max_turns)
        workspace = LocalWorkspace(
            str(self._workspace_root),
            shell='/bin/sh',
            env={},
        )
        tools = local_tools(workspace.portal())
        try:
            with _CaptureRuntimeErrors() as runtime_errors:
                model_names = _model_names(config)
                models: Mapping[str, LLM] = {
                    name: self._model_factory(name) for name in model_names
                }
                facts = HostFacts(
                    os=platform.system().lower(),
                    platform=platform.platform(),
                    shell='/bin/sh',
                )
                factories = catalog.freeze(tools, models, facts)
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
                        topology = _with_turn_limit(
                            topology,
                            request.max_turns,
                            config.catalog,
                        )
                        await runtime.assemble(
                            topology,
                            catalog,
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
                            catalog.get(role)
                            raise AssertionError(
                                'catalog.get returned without a matching runtime factory'
                            )
                        agent_id = await runtime.spawn(
                            factory,
                            query=request.prompt,
                            exit_mode=ExitMode.ZOMBIE,
                        )
                    context = await self._wait_with_pump(runtime, agent_id, pump)
                    return await self._answer(
                        runtime,
                        context,
                        runtime_errors,
                        start=0,
                        fail_without_text=True,
                    )
                finally:
                    await runtime.shutdown()
                    await pump
                    subscription.close()
        finally:
            await workspace.close()
