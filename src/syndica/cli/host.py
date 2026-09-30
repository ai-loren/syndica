"""Runtime-backed implementation of the command-line host protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from textwrap import indent
from types import MappingProxyType
from typing import cast

from syndica.cli.config import CliConfig, load_config
from syndica.cli.dev_module import DevModule
from syndica.cli.environment import AgentEnvironment, agent_environment
from syndica.cli.main import CliIO, InspectRequest, RunRequest
from syndica.cli.policy import dev_danger_options
from syndica.kernel import (
    LLM,
    AgentCatalog,
    AgentOptions,
    AgentSlot,
    AskUser,
    AskUserQuestion,
    ConfigError,
    Context,
    ContractViolation,
    ExitMode,
    FlagDeclaration,
    HookResult,
    HooksCenter,
    HostFacts,
    HumanInteractionModule,
    Junction,
    LocalWorkspace,
    MailboxConfirmationHandler,
    Runtime,
    RuntimeModule,
    SafetyModule,
    StopReason,
    Subscription,
    SyndicaError,
    Tool,
    ToolCall,
    ToolResult,
    Topology,
    TrustLevel,
    local_tools,
    register_builtins,
)
from syndica.kernel.tools import Delegate, Fork, KillAgent, QueryAgent, SendMessage

__all__ = ['RuntimeCliHost']

ModelFactory = Callable[[str], LLM]
_RUNTIME_LOGGER = 'syndica.kernel.runtime'
# Bash 每条输出流最多展示给模型的字符数。整份 pytest 输出进上下文会挤掉别的一切，而汇总行在末尾，
# local_tools 截断时留开头 20%、结尾 80%。
_OUTPUT_LIMIT = 10_000
# Agent 的 Bash 用的 shell。HostFacts 把它告诉模型，两处必须是同一个值。
_SHELL = '/bin/sh'
# CLI 拒收的 Flag 取值：`--flag` 报错，`flags show` 不列。
_REFUSED_FLAG_VALUE = ('untrusted_high_risk', 'allow')
# `run --no-write` 在清单与槽里留下的工具。白名单而不是去掉几个写工具：以后新增的可写工具默认
# 进不来。派活与问人的工具不经清单，由 Runtime 与 Module 另装。
_READ_ONLY_TOOLS = frozenset({'Glob', 'Grep', 'Read'})
# 只读 run 里放行的派活与问人的工具，按确切类型认：同名的替身（自己装一个叫 Delegate 的工具）不算。
_AGENT_TOOL_TYPES: frozenset[type] = frozenset(
    {AskUser, AskUserQuestion, Delegate, Fork, KillAgent, QueryAgent, SendMessage}
)
# 只读 run 交给 Agent 的第一段话前面加这一段：它的系统提示词里仍写着「改文件、跑命令」，不说清楚
# 就只能靠一次次「未知工具」去撞。放在消息里而不是系统提示词里，系统提示词逐轮字节一致。
_READ_ONLY_NOTICE = (
    '（这次 run 是只读的（--no-write）：只有 Read、Grep、Glob 与派活、问人的工具，改文件、跑命令的'
    '工具都用不了。需要改动时，把要改什么、怎么改写进最终回答。）\n\n'
)


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


class _ReadOnlyGuard:
    """`run --no-write` 在 PRE_TOOL_USE 上的闸：只放行宿主交出的三个只读工具实例与派活、问人的工具。

    清单与槽的过滤管不到类式角色在 `install_own_tools` 里自己装的工具（配置里一行 `impl:` 就够），
    也管不到以后新增的工具来源。按对象身份与确切类型认，自己装一个叫 Read 的工具照样拦下。
    """

    def __init__(self, read_tools: tuple[Tool, ...]) -> None:
        self._read_tools = read_tools

    @property
    def name(self) -> str:
        return 'read-only-guard'

    def register(self, hooks: HooksCenter) -> None:
        hooks.register(Junction.PRE_TOOL_USE, self._guard)

    async def _guard(self, **kwargs: object) -> HookResult:
        tool = kwargs['tool']
        if type(tool) in _AGENT_TOOL_TYPES or any(tool is kept for kept in self._read_tools):
            return HookResult()
        name = cast(ToolCall, kwargs['tool_call']).name
        return HookResult(
            skip=True,
            result=ToolResult(
                output=(
                    '已拦截：这次 run 用了 --no-write，只能用 Read、Grep、Glob 与派活、问人的'
                    f'工具；收到的 {name!r} 不在其中。换成只读的做法继续；要改动就把改什么、怎么改'
                    '写进最终回答，由人去掉 --no-write 再跑。'
                ),
                trust=TrustLevel.TRUSTED,
            ),
        )


def _read_only_module(tools: Mapping[str, Tool]) -> RuntimeModule:
    guard = _ReadOnlyGuard(tuple(tools[name] for name in sorted(_READ_ONLY_TOOLS)))

    def plugin(runtime: object, agent: object, flags: object) -> _ReadOnlyGuard:
        del runtime, agent, flags
        return guard

    return RuntimeModule('read-only', agent_plugins=(plugin,))


def _agent_modules(
    workspace_root: Path, read_only_tools: Mapping[str, Tool] | None = None
) -> tuple[RuntimeModule, ...]:
    """run 与 chat 的 Runtime 装的 Module；给了 `read_only_tools` 就另装只读闸。

    开发期 Module 排在最后：可拦截切点按注册顺序串行，前面的 hook 改过的参数传给后面的；排在它
    后面的插件再改参数，守卫判的就是改之前的那一份。
    """
    read_only = () if read_only_tools is None else (_read_only_module(read_only_tools),)
    return (*_modules(), *read_only, DevModule(workspace_root))


def _model_names(config: CliConfig) -> tuple[str, ...]:
    return tuple(dict.fromkeys(config.catalog.get(name).model for name in config.catalog.names()))


def _role_turns(catalog: AgentCatalog, name: str) -> int | None:
    manifest = catalog.get(name)
    return AgentOptions.max_turns if manifest.budget is None else manifest.budget['turns']


def _rebuilt(catalog: AgentCatalog, edit: Callable[[str, dict[str, object]], None]) -> AgentCatalog:
    """同一份目录的一份拷贝，每个角色的清单先交给 edit 改一遍。"""
    rebuilt = AgentCatalog()
    for name in catalog.names():
        manifest = catalog.get(name)
        data: dict[str, object] = {
            field.name: getattr(manifest, field.name)
            for field in fields(manifest)
            if field.name != 'source'
        }
        edit(name, data)
        # system_prompt 已是解析后的正文；留着 origin 会让它被当成相对路径再解析一次。
        rebuilt.load_mapping(data, source=replace(manifest.source, origin=None))
    return rebuilt


def _turn_capped(catalog: AgentCatalog, max_turns: int | None) -> AgentCatalog:
    """同一份目录，但每个角色的轮数预算都不超过 max_turns。

    委派与分叉出来的子 Agent 都由目录里的工厂造出（拓扑的 assemble 也用它覆盖同名工厂），
    所以上限必须落在角色上：只收紧根 Agent 时，一次委派会按角色预算（缺省 50 轮）跑满。
    """
    if max_turns is None:
        return catalog
    limit = max_turns

    def cap(name: str, data: dict[str, object]) -> None:
        role_limit = _role_turns(catalog, name)
        data['budget'] = {'turns': limit if role_limit is None else min(role_limit, limit)}

    return _rebuilt(catalog, cap)


def _read_only(catalog: AgentCatalog) -> AgentCatalog:
    """同一份目录，但每个角色只留只读白名单里的工具。

    编排工具由 Runtime 另装、不在清单里，所以只读 run 里照样能派活；派出去的子 Agent 也由这份
    目录造出，同样只读。类式角色自己装的工具不经清单，由 `_ReadOnlyGuard` 在调用时拦。
    """

    def keep_read_only(_: str, data: dict[str, object]) -> None:
        declared = cast('tuple[str, ...]', data['tools'])
        data['tools'] = [tool for tool in declared if tool in _READ_ONLY_TOOLS]

    return _rebuilt(catalog, keep_read_only)


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
        if (key, value) == _REFUSED_FLAG_VALUE:
            raise _config_error(
                'CLI 的 untrusted_high_risk 是 reject 或 ask',
                '收到 untrusted_high_risk=allow',
                (
                    '改用 --flag untrusted_high_risk=ask（每次问你），或省略它（默认 reject）。'
                    'allow 会让 Agent 读过命令输出之后的每一条高风险命令不经询问就在你的机器上执行'
                ),
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


def _read_only_topology(topology: Topology) -> Topology:
    """每个槽的 extra_tools 只留只读白名单里的：槽不能把写工具加回来。"""
    return Topology(
        slots=tuple(
            replace(
                slot,
                extra_tools=tuple(tool for tool in slot.extra_tools if tool in _READ_ONLY_TOOLS),
            )
            for slot in topology.slots
        )
    )


@dataclass
class _ChatSession:
    config: str
    runtime: Runtime
    workspace: LocalWorkspace
    environment: AgentEnvironment
    agent_id: str
    subscription: Subscription
    pump: asyncio.Task[None]


class RuntimeCliHost:
    """Own Runtime and workspace lifecycles for synchronous console commands.

    `environ` 是宿主自己的环境（缺省 `os.environ`）；Agent 只拿到其中白名单里的那几项，见
    `syndica.cli.environment`。
    """

    def __init__(
        self,
        *,
        io: CliIO,
        model_factory: ModelFactory,
        workspace_root: str | Path,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._io = io
        self._model_factory = model_factory
        self._workspace_root = Path(workspace_root)
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._runner = asyncio.Runner()
        self._closed = False
        self._sessions: dict[str, _ChatSession] = {}

    def run_request(self, request: RunRequest) -> str:
        flags = _validated_request(request)
        if request.session is not None:
            if request.no_write:
                raise _config_error(
                    '--no-write 用于一次新的 run',
                    f'同时收到 session={request.session!r}',
                    '去掉 --session 开一次只读的新 run；会话里的 Agent 建好时已经带着写工具，'
                    '中途收不回来',
                )
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
                    'choices': [
                        choice
                        for choice in declaration.choices
                        if (declaration.key, choice) != _REFUSED_FLAG_VALUE
                    ],
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

    def _open_workspace(self, config: CliConfig) -> tuple[LocalWorkspace, AgentEnvironment]:
        """run、chat、doctor 共用的入口：Agent 在哪条路径上都拿到同一份环境。"""
        environment = agent_environment(
            self._environ, config.workspace.pass_env, workspace_root=self._workspace_root
        )
        try:
            workspace = LocalWorkspace(
                str(self._workspace_root), shell=_SHELL, env=environment.values
            )
        except BaseException:
            environment.remove()
            raise
        return workspace, environment

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
        workspace, environment = self._open_workspace(config)
        try:
            tools = local_tools(workspace.portal(), output_limit=_OUTPUT_LIMIT)
            names = _model_names(config)
            models: Mapping[str, LLM] = {name: self._model_factory(name) for name in names}
            judge, policy = dev_danger_options(self._workspace_root)
            config.catalog.freeze(
                tools,
                models,
                HostFacts(
                    os=platform.system().lower(),
                    platform=platform.platform(),
                    shell=_SHELL,
                ),
                danger_judge=judge,
                danger_policy=policy,
            )
        finally:
            try:
                await workspace.close()
            finally:
                environment.remove()
        return (
            f'config: ok {config.source}\n'
            f'models: ok {", ".join(names)}\n'
            f'workspace: ok {self._workspace_root}\n'
            f'env: {" ".join(environment.values)}'
        )

    async def _open_chat(self, path: str, max_turns: int | None) -> str:
        config = self._config(path)
        catalog = _turn_capped(config.catalog, max_turns)
        workspace, environment = self._open_workspace(config)
        runtime: Runtime | None = None
        subscription: Subscription | None = None
        pump: asyncio.Task[None] | None = None
        try:
            tools = local_tools(workspace.portal(), output_limit=_OUTPUT_LIMIT)
            models: Mapping[str, LLM] = {
                name: self._model_factory(name) for name in _model_names(config)
            }
            judge, policy = dev_danger_options(self._workspace_root)
            factories = catalog.freeze(
                tools,
                models,
                HostFacts(
                    os=platform.system().lower(),
                    platform=platform.platform(),
                    shell=_SHELL,
                ),
                danger_judge=judge,
                danger_policy=policy,
            )
            runtime = Runtime(
                agents=factories,
                orchestration=True,
                modules=_agent_modules(self._workspace_root),
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
                environment=environment,
                agent_id=agent_id,
                subscription=subscription,
                pump=pump,
            )
            return agent_id
        except BaseException:
            try:
                if runtime is not None:
                    await runtime.shutdown()
                if pump is not None:
                    await asyncio.gather(pump, return_exceptions=True)
                if subscription is not None:
                    subscription.close()
                await workspace.close()
            finally:
                environment.remove()
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
            # 读人类输入的泵失败时，它的异常在那一轮已经交给了调用方；收尾再抛一次会让后面的
            # 工作区与临时目录都不再被收拾。
            try:
                await session.runtime.shutdown()
                await asyncio.gather(session.pump, return_exceptions=True)
                session.subscription.close()
                await session.workspace.close()
            finally:
                session.environment.remove()

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
        prompt = _READ_ONLY_NOTICE + request.prompt if request.no_write else request.prompt
        workspace, environment = self._open_workspace(config)
        tools = local_tools(workspace.portal(), output_limit=_OUTPUT_LIMIT)
        try:
            with _CaptureRuntimeErrors() as runtime_errors:
                model_names = _model_names(config)
                models: Mapping[str, LLM] = {
                    name: self._model_factory(name) for name in model_names
                }
                facts = HostFacts(
                    os=platform.system().lower(),
                    platform=platform.platform(),
                    shell=_SHELL,
                )
                judge, policy = dev_danger_options(self._workspace_root)
                factories = catalog.freeze(
                    tools, models, facts, danger_judge=judge, danger_policy=policy
                )
                if request.no_write:
                    # 先按正常 run 冻结了一遍：过滤会把拼错的工具名一起滤掉，只读 run 不该放过
                    # 正常 run 会拒的配置。
                    catalog = _read_only(catalog)
                    factories = catalog.freeze(
                        tools, models, facts, danger_judge=judge, danger_policy=policy
                    )
                runtime = Runtime(
                    agents={} if request.topology is not None else factories,
                    orchestration=True,
                    modules=_agent_modules(
                        self._workspace_root, tools if request.no_write else None
                    ),
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
                        if request.no_write:
                            topology = _read_only_topology(topology)
                        await runtime.assemble(
                            topology,
                            catalog,
                            tools=tools,
                            models=models,
                            host=facts,
                            danger_judge=judge,
                            danger_policy=policy,
                        )
                        delivered = await runtime.send_to_slot(root_slot.name, prompt)
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
                            query=prompt,
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
            try:
                await workspace.close()
            finally:
                environment.remove()
