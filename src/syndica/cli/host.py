"""Runtime-backed implementation of the command-line host protocol."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from textwrap import indent
from types import MappingProxyType
from typing import Any, TextIO, TypeVar, cast

from syndica import __version__
from syndica.cli.capture import frozen_roles, host_profile
from syndica.cli.checks import parse_check
from syndica.cli.config import CliConfig, load_config, yaml_file
from syndica.cli.dev_module import DevLog, DevLogSnapshot, DevModule, last_answer
from syndica.cli.environment import AgentEnvironment, agent_environment
from syndica.cli.executor import DaemonExecutor
from syndica.cli.export import (
    TraceExporter,
    command_line_target,
    environment_resource,
    environment_target,
)
from syndica.cli.flags import flag_details, flag_lines, parse_assignments, resolved_flags
from syndica.cli.git_finish import (
    Finish,
    GitInterrupted,
    Integrity,
    finish,
    hook_environment,
    ignored_paths,
    integrity,
)
from syndica.cli.handoff import (
    HandoffFacts,
    HandoffLocked,
    HandoffWriteError,
    RedactingWriter,
    VerifyResult,
    artifact_root,
    commit_title,
    exit_code_of,
    front_matter,
    lock_workspace,
    memory_root,
    path_key,
    protected_matcher,
    render,
    reviewer_verdict,
    rewrite_handoff,
    rubric_root,
    session_root,
    status_of,
    trace_directory,
    trace_root,
    write_handoff,
)
from syndica.cli.host_git import Change, GitUnavailable, HostGit
from syndica.cli.main import (
    EXIT_INTERRUPTED,
    CliIO,
    CliOutcome,
    InspectRequest,
    MemoryRequest,
    RunRequest,
    SessionsRequest,
    TraceRequest,
)
from syndica.cli.modules import (
    OPTIONAL_BUILTINS,
    EntryPointSource,
    discover,
    load_module,
    module_rows,
    no_entry_points,
    runs_code,
    third_party,
    validated_modules,
)
from syndica.cli.policy import dev_danger_options, role_policies
from syndica.cli.recording import (
    MirroredTraceStore,
    Recording,
    described,
    run_status,
    start_recording,
    trace_level,
    trace_source,
)
from syndica.cli.redaction import host_redactor
from syndica.cli.session_lock import SessionLock, acquire_session_lock
from syndica.cli.terminal import one_line, terminal_safe
from syndica.cli.trace_commands import (
    Replayer,
    TraceUnusable,
    trace_export,
    trace_list,
    trace_prune,
    trace_replay,
    trace_show,
    unusable_replay,
)
from syndica.cli.trust import (
    ConfigSnapshot,
    PinnedImports,
    TrustGate,
    TrustStore,
    code_location,
    config_of,
    describe,
    private_pycache_prefix,
    read_snapshot,
    within,
)
from syndica.kernel import (
    LLM,
    AgentCatalog,
    AgentFactory,
    AgentSlot,
    AskUser,
    AskUserQuestion,
    Budget,
    BudgetPolicy,
    BudgetView,
    ConfigError,
    Context,
    ContractViolation,
    DangerPolicy,
    ExecResult,
    Exhaustion,
    ExitMode,
    FileSessionStore,
    FileTaint,
    FileTraceStore,
    HookResult,
    HooksCenter,
    HostFacts,
    HumanInteractionModule,
    Junction,
    LocalIsolator,
    LocalWorkspace,
    MailboxConfirmationHandler,
    ModuleHost,
    Portal,
    PrefixDriftModule,
    PriceTable,
    RetryModule,
    Runtime,
    RuntimeModule,
    SafetyModule,
    SessionMeta,
    SessionStore,
    SpanStatus,
    StaticPrices,
    StopReason,
    Subscription,
    SyndicaError,
    Tool,
    ToolCall,
    ToolResult,
    Topology,
    TraceLevel,
    TraceRecorder,
    TrustLevel,
    VersionStore,
    WorkspaceRouter,
    flag_reference,
    local_tools,
    register_builtins,
)
from syndica.kernel.acceptance import RubricStatus, Verdict, VerdictStatus
from syndica.kernel.acceptance_module import AcceptanceModule
from syndica.kernel.background_module import BackgroundModule
from syndica.kernel.compaction_module import CompactionModule
from syndica.kernel.long_horizon_module import LongHorizonModule
from syndica.kernel.memory_module import MemoryModule
from syndica.kernel.memory_redaction import redacted_memory_sections
from syndica.kernel.memory_types import MemoryScope
from syndica.kernel.redaction import Redactor
from syndica.kernel.scenario_schema import FROZEN_IMPLS
from syndica.kernel.stores.file_artifact import FileArtifactStore
from syndica.kernel.stores.file_memory import FileMemoryStore
from syndica.kernel.stores.file_rubric import FileRubricStore
from syndica.kernel.stores.memory_memory import MemoryConflict
from syndica.kernel.tools import Delegate, Fork, KillAgent, QueryAgent, SendMessage

__all__ = ['RuntimeCliHost', 'reference_modules']

ModelFactory = Callable[[str], LLM]
_RUNTIME_LOGGER = 'syndica.kernel.runtime'
# Bash 每条输出流、Glob 与 Grep 每次结果最多展示给模型的字符数。整份 pytest 输出进上下文会挤掉别的
# 一切，而汇总行在末尾，local_tools 截断时留开头 20%、结尾 80%；一次宽泛的 Grep 同理。
_OUTPUT_LIMIT = 10_000
# Runtime 的装配：构造 Runtime 与写回放文档的宿主档案用同一份，回放按它重建。
_ORCHESTRATION = True
# 不给 --max-turns 时整棵派活树的轮数（计划 P1 H2）：每个子 Agent 至少切一轮，于是树的规模有界，且
# 增长都能归因到切分。
_DEFAULT_ROOT_TURNS = 200
_CONFIRMATION_TIMEOUT = 300.0
# Agent 的 Bash 用的 shell。HostFacts 把它告诉模型，两处必须是同一个值。
_SHELL = '/bin/sh'
# 文件工具碰不到的目录：trace 里存着此前标成不可信的工具输出，经 Read 读回来就成了可信内容；交接
# 文件只由宿主写；会话 / 记忆 / 产物 / Rubric 存着 trust 标记与验收状态，Agent 经 Write 改它们能把
# 不可信内容写成可信、或把 proposed Rubric 改成 active 给自己出题（与 danger._HOST_RECORDS 同一份
# 名单）。不整个关 `.syndica`：Worker 把长输出存进 `.syndica/tmp/`。
_PRIVATE = (
    '.syndica/traces',
    '.syndica/handoffs',
    '.syndica/vcs',
    '.syndica/worktrees',
    '.syndica/snapshots',
    '.syndica/sessions',
    '.syndica/memory',
    '.syndica/artifacts',
    '.syndica/rubrics',
)
_INCOMPLETE_TRACE = 'trace（不完整，原因见上面的 ERROR；不要拿它回放）'
# `run --no-write` 在清单与槽里留下的工具。白名单而不是去掉几个写工具：以后新增的可写工具默认
# 进不来。派活与问人的工具不经清单，由 Runtime 与 Module 另装。
_READ_ONLY_TOOLS = frozenset({'Glob', 'Grep', 'Read'})
_READ_ONLY_MANIFEST: dict[str, Any] = {
    'version': __version__,
    'description': '--no-write：只放行只读、派活与问人的工具',
    'capabilities': (),
    'replay_safe': True,
}
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
# 宿主复核每条命令的上限（D13）：比 Agent 的 Bash 宽得多，全量测试要跑得完。
_VERIFY_TIMEOUT = 900.0
# doctor 跑 `command -v` 与 `<exe> --version` 的上限。
_PROBE_TIMEOUT = 30.0
# 记录开始时问一次 HEAD 的上限：问不出来就不记，不为它拖住 run。
_HEAD_TIMEOUT = 5.0
# run 结束时导出的总上限：一个慢的 collector 按批收，每批都在单次请求的超时以内，也能把命令行拖住。
_EXPORT_DEADLINE = 30.0
_COMMIT_ID = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
# 能不执行就找出来的程序名：普通名字或路径，外加 `[`；带 shell 语法或展开的词不在其中。
_PROGRAM = re.compile(r'\[|[A-Za-z0-9_./+@%,:-]+')
# 交接正文「Agent 跑过的检查」不列这一类：echo、ls 不是检查。
_UNRECOGNISED_CHECK = 'other'


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


def _read_only_manifest() -> RuntimeModule:
    """只读闸的清单（`modules show` 用）：插件要拿 run 的只读工具实例才建得出来。"""
    return RuntimeModule('read-only', **_READ_ONLY_MANIFEST)


def _manifest(module: RuntimeModule) -> dict[str, object]:
    """`modules show` 的输出：清单字段与它声明的 Flag。能力未声明写「未声明」。"""
    return {
        'name': module.name,
        'version': module.version,
        'description': module.description,
        'capabilities': '未声明' if module.capabilities is None else list(module.capabilities),
        'requires': list(module.requires),
        'junctions': [
            {'name': junction.name, 'kind': junction.kind.value} for junction in module.junctions
        ],
        'replay_safe': module.replay_safe,
        'security_notes': module.security_notes,
        'flags': [flag.key for flag in module.flags],
    }


def _read_only_module(tools: Mapping[str, Tool]) -> RuntimeModule:
    guard = _ReadOnlyGuard(tuple(tools[name] for name in sorted(_READ_ONLY_TOOLS)))

    def plugin(runtime: object, agent: object, flags: object) -> _ReadOnlyGuard:
        del runtime, agent, flags
        return guard

    return RuntimeModule('read-only', agent_plugins=(plugin,), **_READ_ONLY_MANIFEST)


def _agent_modules(
    workspace_root: Path,
    environ: Mapping[str, str],
    read_only_tools: Mapping[str, Tool] | None = None,
    configured: Sequence[RuntimeModule] = (),
) -> tuple[RuntimeModule, ...]:
    """run 与 chat 的 Runtime 装的 Module：必装的、配置里启用的、只读闸（给了 `read_only_tools`
    时）、开发期 Module。

    开发期 Module 排在最后：可拦截切点按注册顺序串行，前面的 hook 改过的参数传给后面的；排在它
    后面的插件再改参数，守卫判的就是改之前的那一份。
    """
    read_only = () if read_only_tools is None else (_read_only_module(read_only_tools),)
    return (*_modules(), *configured, *read_only, DevModule(workspace_root, environ))


def _shutdown_failure(error: Exception) -> str:
    parts: tuple[BaseException, ...] = (
        tuple(cast('BaseExceptionGroup[BaseException]', error).exceptions)
        if isinstance(error, BaseExceptionGroup)
        else (error,)
    )
    described = '；'.join(
        f'{type(part).__name__}: {part}'
        + ''.join(f'（{note}）' for note in getattr(part, '__notes__', ()))
        for part in parts
    )
    return (
        f'期望: Runtime 关闭时每个 Module 的 on_shutdown 都正常结束。 实际: {described}。'
        ' 下一步: 修好那个 Module 的 on_shutdown，或从 modules 里删掉它；这次 run 的结论照常交出。'
    )


def _require_enabled(name: str, config: CliConfig) -> None:
    if name not in config.modules:
        raise _config_error(
            '代码在工作区之内的第三方 Module 先在 modules 里启用、经信任确认才 import',
            f'{name!r} 的代码在工作区之内（Agent 写得到），这份配置没有启用它',
            f'在 modules 里写上 {name!r}、确认配置之后再查看',
        )


def reference_modules() -> tuple[RuntimeModule, ...]:
    """参考文档的 Flag 来源：宿主必装与可选的内置 Module，按装配次序。不含第三方：参考文档不随
    谁的环境里装了什么而变。"""
    return (*_modules(), *(_optional_builtin(name) for name in OPTIONAL_BUILTINS))


def _optional_builtin(name: str) -> RuntimeModule:
    """按名字启用的内置 Module。`retry` 没有备用模型：CLI 不给 fallbacks。记忆这里用默认（内存
    store、无工作区），只供 `flags list` / 参考文档读声明；真正跑时 `_configured_modules` 换成带
    工作区与 FileMemoryStore 的那一份。"""
    if name == 'retry':
        return RetryModule({})
    if name == 'compaction':
        return CompactionModule()
    if name == 'memory':
        return MemoryModule()
    if name == 'acceptance':
        return AcceptanceModule()
    if name == 'long-horizon':
        return LongHorizonModule(AcceptanceModule())
    if name == 'background':
        return BackgroundModule()
    return PrefixDriftModule()


def _model_names(config: CliConfig) -> tuple[str, ...]:
    return tuple(dict.fromkeys(config.catalog.get(name).model for name in config.catalog.names()))


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


def _syndica_cause(record: logging.LogRecord) -> SyndicaError | None:
    error = record.exc_info[1] if record.exc_info is not None else None
    return error if isinstance(error, SyndicaError) else None


def _runtime_error_report(
    record: logging.LogRecord, names: Mapping[str, str], position: str
) -> str:
    args = record.args if isinstance(record.args, tuple) else ()
    agent = next((arg for arg in args if isinstance(arg, str) and arg in names), None)
    where = '' if agent is None else f'（agent {names[agent]} {agent}）'
    return f'Runtime 错误 {position}{where}:\n{indent(_record_detail(record), "  ")}'


class _AgentFailure(ContractViolation):
    """目标 Agent 以错误结束：它自己那个错误的三件套，外加一行说明是哪个 Agent（`headline`）。
    CLI 与交接文件都把这一行放在三件套前面：分开写的话，交接模式下这一行进 stderr、三件套进交接
    文件，两边各缺一半。"""

    def __init__(
        self, *, expected: str, received: str, next_action: str, headline: str = ''
    ) -> None:
        super().__init__(expected=expected, received=received, next_action=next_action)
        self.headline = headline


def _own_failure(records: list[logging.LogRecord], agent_id: str) -> logging.LogRecord | None:
    """这个 Agent 自己以异常结束时 Runtime 记下的那一条：参数里的第一个是它的 id，且带着异常。
    之后别的 Agent 的失败、它自己收尾时没带异常的告警（被丢下的输入）都不是它的结局。"""
    for record in reversed(records):
        args = record.args if isinstance(record.args, tuple) else ()
        if args and args[0] == agent_id and record.exc_info is not None:
            return record
    return None


def _failure_text(exc: BaseException) -> str:
    """一个失败写进交接文件时的文字：带 `headline` 的把它放在三件套前面。"""
    headline = getattr(exc, 'headline', None)
    return f'{headline}\n{exc}' if isinstance(headline, str) and headline else str(exc)


def _error_failure(final: logging.LogRecord | None) -> ContractViolation:
    received = 'Context.stop_reason 是 error'
    next_action = '查看当前命令之前输出的 Runtime 错误日志，修复失败的 LLM、工具或 hook'
    if final is not None:
        received = f'{received}；Runtime 最近错误：{_record_detail(final)}'
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
    StopReason.BUDGET: (
        '它的预算用完了，收尾的那一轮也没有留下文字',
        '提高 --max-turns / --max-tokens / --max-cost / --max-wall-clock 后重试，或把任务拆小',
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
    StopReason.BUDGET: (
        '注意: Agent 的预算用完了，本次输出是它收尾时交出的那一段，未完成的部分写在里面；'
        '需要做完就提高 --max-turns / --max-tokens / --max-cost / --max-wall-clock 重试'
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


def _session_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime('%Y-%m-%d %H:%M:%SZ')


def _session_absent(sid: str) -> ConfigError:
    return _config_error(
        f'会话 {sid} 在 .syndica/sessions 里',
        '没有这个会话',
        '用 sessions list 看现有会话 id（id 就是根 Agent 的 id）',
    )


def _session_row(meta: SessionMeta) -> str:
    # 这些字段来自 Agent 改得到的 meta.json：转义后再进人的终端，挡 ESC / OSC 注入。
    title = one_line(terminal_safe(meta.title)) if meta.title else '(无标题)'
    agent = one_line(terminal_safe(meta.agent))
    return (
        f'{_session_time(meta.updated_at)}  {meta.sid}  {meta.status.value}  '
        f'{agent}  turns={meta.turns}  {title}'
    )


async def _sessions_list(store: SessionStore) -> str:
    metas = await store.list_metas()
    if not metas:
        return '（没有会话）'
    return '\n'.join(_session_row(meta) for meta in metas)


async def _sessions_show(store: SessionStore, sid: str) -> str:
    record = await store.load(sid)
    if record is None:
        raise _session_absent(sid)
    meta = record.meta

    # Agent 改得到的字段一律转义后再打印（评审 M8）。
    def safe(text: str) -> str:
        return one_line(terminal_safe(text))

    lines = [
        f'会话 {meta.sid}',
        f'  agent:   {safe(meta.agent)}',
        f'  状态:    {meta.status.value}',
        f'  轮数:    {meta.turns}',
        f'  model:   {safe(record.model)}',
        f'  tools:   {safe(", ".join(record.tools))}',
        f'  创建:    {_session_time(meta.created_at)}',
        f'  更新:    {_session_time(meta.updated_at)}',
        f'  归档数:  {meta.archived}',
        f'  块数:    {len(record.content)}',
    ]
    if meta.title:
        lines.insert(1, f'  标题:    {safe(meta.title)}')
    return '\n'.join(lines)


def _rubric_line(text: str) -> str:
    return one_line(terminal_safe(text))


async def _session_rubrics(store: FileRubricStore, sid: str) -> str:
    """`sessions rubrics <sid>`：这份会话的 Goal 与全部 Rubric（含提议）。模型写的文字一律转义。"""
    goal = await store.get_goal(sid)
    rubrics = sorted(await store.rubrics(sid, include_proposed=True), key=lambda r: r.id)
    if goal is None and not rubrics:
        raise _config_error(
            f'会话 {sid} 有验收记录（Goal 或 Rubric）',
            '它一条也没有：这次 run 没装 acceptance，或会话 id 不对',
            '用 sessions list 看会话 id；要验收就在 modules 里启用 acceptance',
        )
    lines = [f'会话 {sid} 的验收标准']
    if goal is not None:
        lines.append(f'  Goal: {_rubric_line(goal.intent)[:200]}')
    for rubric in rubrics:
        lines.append(
            f'  {_rubric_line(rubric.id)}  {rubric.status.value}  {rubric.kind.value}  '
            f'{_rubric_line(rubric.statement)}'
        )
    if any(rubric.status is RubricStatus.PROPOSED for rubric in rubrics):
        lines.append(f'  让一条提议生效：syndica sessions activate {sid} <rubric-id>')
    return '\n'.join(lines)


async def _session_activate(store: FileRubricStore, sid: str, rubric_id: str) -> str:
    """`sessions activate <sid> <rubric-id>`：人确认一条提议（§7.7）。store 的写在 `flock` 下做
    read-modify-write，正在跑的会话不必先停：它下一个周期起就按这条验收。"""
    current = {rubric.id: rubric for rubric in await store.rubrics(sid, include_proposed=True)}
    target = current.get(rubric_id)
    if target is None or target.status is not RubricStatus.PROPOSED:
        found = '没有这一条' if target is None else '这一条已经生效'
        raise _config_error(
            f'activate 指向会话 {sid} 里一条 proposed Rubric',
            f'{_rubric_line(rubric_id)!r}：{found}',
            f'用 syndica sessions rubrics {sid} 看有哪些提议',
        )
    await store.activate(sid, rubric_id)
    return f'已生效：{_rubric_line(rubric_id)}。此后每一次验收都按它查'


async def _memory_list(store: FileMemoryStore, scope: MemoryScope, workspace: str | None) -> str:
    """`memory list`：长期记忆的各段（召回注入的就是它们）与还没整理的短期记录。文字是模型写的，
    转义、截短后再打印。"""
    record = await store.read(scope, workspace)
    pending = await store.entries(
        scope, workspace, after_seq=record.through_seq if record is not None else 0
    )
    lines = [f'{scope.value} 层长期记忆（每个会话开头注入）：']
    sections = record.sections if record is not None else []
    for position, section in enumerate(sections, start=1):
        lines.append(
            f'  {position}. [{section.trust.value}] {_rubric_line(section.heading)}：'
            f'{_rubric_line(section.text)[:160]}'
        )
    if not sections:
        lines.append('  （没有）')
    untrusted = sum(1 for entry in pending if entry.trust is TrustLevel.UNTRUSTED)
    lines.append(f'还没整理的短期记录 {len(pending)} 条（不可信 {untrusted} 条）')
    if sections or pending:
        lines.append(
            '忘掉一段：syndica memory forget <段号>；丢掉短期记录：syndica memory forget --pending'
        )
    return '\n'.join(lines)


async def _memory_forget(
    store: FileMemoryStore,
    scope: MemoryScope,
    workspace: str | None,
    request: MemoryRequest,
    redactor: Redactor,
) -> str:
    """`memory forget`：不可信的记忆一旦整理进长期层，就进了此后每个会话；这是人撤回它的入口
    （M6 收口评审 M4）。按读到的版本重写（乐观并发）：这期间又被整理过就报冲突、不覆盖。"""
    record = await store.read(scope, workspace)
    sections = list(record.sections) if record is not None else []
    through = record.through_seq if record is not None else 0
    version = record.version if record is not None else 0
    if request.pending:
        pending = await store.entries(scope, workspace, after_seq=through)
        if not pending:
            return '没有还没整理的短期记录'
        kept, through, done = (
            sections,
            pending[-1].seq,
            f'丢掉了 {len(pending)} 条还没整理的短期记录',
        )
    else:
        index = request.index
        if index is None or not 1 <= index <= len(sections):
            raise _config_error(
                f'memory forget 的段号是 memory list 里的一个（1 到 {len(sections)}）',
                f'收到 {index!r}',
                '先 syndica memory list 看段号',
            )
        kept = [section for position, section in enumerate(sections, 1) if position != index]
        done = f'忘掉了第 {index} 段：{_rubric_line(sections[index - 1].heading)}'
    try:
        await store.consolidate(
            redacted_memory_sections(scope, workspace, kept, through, redactor),
            expected_version=version,
        )
    except MemoryConflict:
        raise _config_error(
            '记忆在读与写之间没有被别人整理过',
            '一个正在跑的会话刚刚又整理了一次',
            '重新 syndica memory list 看段号，再忘一次',
        ) from None
    return done


def _verdict_line(verdict: Verdict) -> str:
    """终态裁决的一行（stderr）。不是 pass 就说清为什么；模型写的理由经转义、截短。"""
    parts = [f'验收：{verdict.status.value}']
    if verdict.unverified_cause is not None:
        parts.append(f'没能验证（{verdict.unverified_cause}）')
    if verdict.needs_human:
        parts.append(f'需要人确认 {", ".join(verdict.needs_human)}')
    if verdict.status is not VerdictStatus.PASS and verdict.reason:
        parts.append(verdict.reason[:200])
    return _rubric_line('；'.join(parts))


async def _sessions_rm(
    store: SessionStore, sessions_root: Path, sid: str, rubrics: FileRubricStore | None = None
) -> str:
    if not await store.exists(sid):
        raise _session_absent(sid)
    # 删之前先抢锁：会话正被别的进程续着 / 跑着时删，下一次 checkpoint 会把目录重建、created_at /
    # title 清零，删除被静默撤销（评审 H2）。抢到就删、随即放；抢不到报三件套拒绝。
    lock = acquire_session_lock(sessions_root, sid)
    try:
        await store.delete(sid)
        if rubrics is not None:
            await rubrics.forget(sid)
    finally:
        lock.release()
    return f'已删除会话 {sid}'


def _root_budget_record(policy: BudgetPolicy) -> dict[str, object]:
    """回放文档 v4 的 `host.root_budget`：回放按它重建同一个根预算。成本与墙钟的 run 不写回放文档
    （见 `_unreplayable_budget`），这里只记得下轮次与 token。"""
    root = policy.root
    return {
        'turns': root.turns,
        'tokens': root.tokens,
        'on_exhausted': policy.on_exhausted.value,
    }


def _unreplayable_budget(request: RunRequest) -> str | None:
    if request.max_cost is not None:
        return '设了 --max-cost：成本的判定取决于宿主的价格表，回放文档不记它'
    if request.max_wall_clock is not None:
        return '设了 --max-wall-clock：墙钟预算的判定取决于真实时间，回放复现不了'
    if request.on_exhausted == Exhaustion.ASK_HUMAN.value:
        return '用了 --on-exhausted ask_human：人追加了多少不在回放文档里'
    return None


def _budget_line(view: BudgetView, reason: StopReason | None) -> str:
    """run 结束时 stderr 上的那一行（spec §11.4：长跑的预算、进度、停止原因都可观测）。"""
    used, limit = view.used, view.limit
    turns = f'{used.turns} / {limit.turns}' if limit.turns is not None else f'{used.turns}'
    tokens = f'输入 {used.input_tokens}（缓存命中 {used.cached_input_tokens}）'
    tokens += f'、输出 {used.output_tokens}'
    if limit.tokens is not None:
        tokens += f'，上限 {limit.tokens}'
    cost = '未知' if used.cost is None else str(used.cost)
    if limit.cost is not None:
        cost += f' / {limit.cost}'
    estimated = '（含按字符估算的 token）' if used.estimated else ''
    ended = '—' if reason is None else reason.value
    return f'预算：轮次 {turns}；token {tokens}{estimated}；成本 {cost}；停止原因 {ended}'


def _validated_request(request: RunRequest) -> dict[str, str]:
    if request.agent is not None and request.topology is not None:
        raise _config_error(
            'run 只选择 --agent 或 --topology 之一',
            f'同时收到 agent={request.agent!r} topology={request.topology!r}',
            '删除其中一个目标参数后重试',
        )
    # 取值要等装好 Module（配置里启用了哪些）才对着声明判；这里先挡住形状不对的。
    return parse_assignments(request.flags)


def _assembled_topology(
    config: CliConfig, request: RunRequest
) -> tuple[Topology, AgentSlot] | None:
    """`--topology` 实际装配的那一份（CLI 改写过预算与只读工具之后）与它的根槽；不是拓扑 run 时
    None。回放文档记的就是这一份：记配置里的原样，回放就按另一套预算与工具跑。"""
    if request.topology is None:
        return None
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
    if request.no_write:
        topology = _read_only_topology(topology)
    root = next(slot for slot in topology.slots if slot.name == roots[0].name)
    return topology, root


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


def _changes(git: HostGit) -> tuple[tuple[Change, ...] | None, str | None]:
    """(改动, 取不到时的原因)。不是 git 仓库、没有 git 都不让交接失败，只让它写明原因。"""
    try:
        return git.changes(), None
    except GitUnavailable as exc:
        return None, str(exc)


def _differences(
    before: tuple[Change, ...] | None, after: tuple[Change, ...] | None
) -> tuple[str, ...]:
    """两份快照之间内容或状态不同的路径（多出来的、少了的都算）。"""
    if before is None or after is None:
        return ()
    old = {change.path: change for change in before}
    new = {change.path: change for change in after}
    return tuple(sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path)))


def _touched_again(
    before: tuple[Change, ...] | None, after: tuple[Change, ...] | None
) -> tuple[str, ...]:
    """运行前就改过、这次又被改（或被改回去）的文件：提交会把人自己的改动一起带走。"""
    if before is None or after is None:
        return ()
    new = {change.path: change for change in after}
    return tuple(sorted(change.path for change in before if new.get(change.path) != change))


def _written(log: DevLogSnapshot, root: Path) -> frozenset[str]:
    """Write / Edit 写成了的路径，按工作区的解析方式落到真实位置（符号链接目录、`/tmp` 与
    `/private/tmp` 这样的两种写法都归一），再换成相对工作区根的比较键，好与 git 的改动逐条对上。"""
    real_root = os.path.realpath(root)
    keys: set[str] = set()
    for entry in log.tools:
        if entry.tool in {'Write', 'Edit'} and entry.target:
            target = Path(entry.target)
            full = os.path.realpath(target if target.is_absolute() else root / target)
            keys.add(path_key(Path(os.path.relpath(full, real_root)).as_posix()))
    return frozenset(keys)


def _run_changes(
    before: tuple[Change, ...] | None, after: tuple[Change, ...] | None
) -> tuple[Change, ...]:
    """这次 run 改出来的：快照里与运行前那一份不同的条目。运行前那份没取到时只能当全是这次的。"""
    if after is None:
        return ()
    if before is None:
        return after
    old = {change.path: change for change in before}
    return tuple(change for change in after if old.get(change.path) != change)


def _in_repository(git: HostGit) -> bool:
    try:
        return git.run('rev-parse', '--is-inside-work-tree').strip() == b'true'
    except GitUnavailable:
        return False


def _checks(log: DevLogSnapshot) -> tuple[str, ...]:
    return tuple(
        f'`{entry.target}`：{entry.check.status}'
        + (f'（{entry.check.summary}）' if entry.check.summary else '')
        for entry in log.tools
        if entry.check is not None and entry.check.tool != _UNRECOGNISED_CHECK
    )


def _next_steps(
    status: str,
    host_verify: str,
    verdict: str,
    results: tuple[VerifyResult, ...],
    changed_by_verify: tuple[str, ...],
    verify_error: str | None,
    *,
    changed: bool,
) -> tuple[str, ...]:
    """「下一步」由宿主按状态生成，不取模型的文字。"""
    if status == 'run_failed':
        if not changed:
            return ('run 没有正常结束：原因写在「Agent 的结论」一节开头，修好之后重跑。',)
        return (
            'run 没有正常结束：原因写在「Agent 的结论」一节开头，修好之后重跑。工作区里的改动还在，'
            '没有提交。',
        )
    if status == 'read_only':
        return ('只读 run 不改动工作区，也不做宿主复核与 git 收尾。',)
    if status == 'verified' and not changed:
        return ('宿主复核全过、Reviewer 结论为 pass，但这次没有改动：不需要提交。',)
    if status == 'verified':
        return ('宿主复核全过、Reviewer 结论为 pass：可以提交，见「git 收尾」。',)
    steps: list[str] = []
    for result in results:
        if not result.passed:
            steps.append(
                f'修好复核命令 `{one_line(result.command)}`（{result.summary}）：在工作区里'
                '手动跑它，修到通过之后重跑 `syndica run --handoff`。'
            )
    if verify_error is not None:
        steps.append(
            '复核没法下结论（见「宿主复核」）：修好那个原因之后重跑 `syndica run --handoff`。'
        )
    if changed_by_verify:
        steps.append(
            '复核命令改了工作区里的文件：让它们只读地跑，或把它们生成的文件加进 .gitignore。'
        )
    if host_verify == 'not_configured':
        steps.append('在 .syndica.yaml 的 workspace.verify 里配上本仓的检查命令，宿主才会复核。')
    if status == 'unverified' and verdict != 'pass':
        steps.append(
            '没有可用的 Reviewer pass 结论（没派 Reviewer、它之后又有改动、它自己改了文件，或结论'
            '不是 pass）：人工 review 改动之后再提交。'
        )
    return tuple(steps)


def _git_line(git: HostGit) -> str:
    """doctor 的 git 一行：只是情况说明，不让 doctor 失败。「不是仓库」只认 git 自己这么说；git
    太旧、`safe.directory` 拒绝这类失败照实写出来——交接时会把这样的仓库当成不是仓库，只有这里
    看得见。"""
    try:
        probe = git.attempt('rev-parse', '--is-inside-work-tree')
    except GitUnavailable as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return 'git: 没有 git（宿主的 PATH 里找不到它）'
        return f'git: 用不了（{one_line(str(exc))}）'
    if probe.returncode != 0:
        stderr = probe.stderr.decode('utf-8', 'replace').strip()
        if 'not a git repository' in stderr:
            return 'git: 非 git 仓库'
        return (
            f'git: 用不了（`git rev-parse` 退出 {probe.returncode}：{one_line(stderr)}）。下一步：'
            '确认宿主的 git 是 2.40 以上，且这个仓库属于你（safe.directory）'
        )
    if probe.stdout.strip() != b'true':
        return 'git: 非 git 仓库'
    try:
        changes = git.changes()
    except GitUnavailable as exc:
        return f'git: 列不出改动（{one_line(str(exc))}）'
    return 'git: clean' if not changes else f'git: {len(changes)} 处改动'


def _verify_executables(
    commands: Sequence[str],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    """verify 用到的可执行文件（每条用 shlex 拆开、跳过开头的 `NAME=值`、取第一个词，按首次出现
    去重）、拆不开的命令与原因，以及不单独检查的词：带 shell 语法或展开的（`(cd`、`$TOOL`）、
    这条命令自己改了 PATH 的——它们指的是哪个程序，不执行就说不准。"""
    found: dict[str, None] = {}
    unchecked: dict[str, None] = {}
    unparsed: list[tuple[str, str]] = []
    for command in commands:
        try:
            words = shlex.split(command)
        except ValueError as exc:
            unparsed.append((command, str(exc)))
            continue
        assignments = list(itertools.takewhile(_ASSIGNMENT.match, words))
        rest = words[len(assignments) :]
        if not rest:
            continue
        sets_path = any(word.split('=', 1)[0] == 'PATH' for word in assignments)
        if sets_path or not _PROGRAM.fullmatch(rest[0]):
            unchecked.setdefault(rest[0])
        else:
            found.setdefault(rest[0])
    return tuple(found), tuple(unparsed), tuple(name for name in unchecked if name not in found)


async def _locate(portal: Portal, name: str) -> tuple[str | None, ExecResult]:
    """`command -v` 在 Agent 的环境里找这个程序：交回它解析到的位置（内建命令是名字本身，找不到是
    None）与那次结果。只查不跑。"""
    found = await portal.run(f'command -v {shlex.quote(name)}', timeout=_PROBE_TIMEOUT, cwd='.')
    lines = found.stdout.strip().splitlines() if _ran(found) else []
    return (lines[0].strip() if lines else None), found


def _why_not_found(found: ExecResult) -> str:
    return f'：{_how_it_failed(found)}' if found.timed_out else ''


def _inside_workspace(where: str, root: Path) -> bool:
    """`command -v` 给的位置是不是在工作区里：相对路径（`./check.sh`）从工作区根算。"""
    real_root = os.path.realpath(root)
    full = os.path.realpath(where if os.path.isabs(where) else os.path.join(root, where))
    return full == real_root or full.startswith(real_root + os.sep)


def _ran(result: ExecResult) -> bool:
    return result.exit_code == 0 and not result.timed_out


def _first_line(result: ExecResult) -> str:
    """版本号：stdout 或 stderr 的第一个非空行（有的程序把版本打在 stderr），转义、截到 80 个
    字符。"""
    text = next(
        (line for line in (result.stdout + '\n' + result.stderr).splitlines() if line.strip()), ''
    )
    text = one_line(text.strip())
    return text if len(text) <= 80 else text[:79] + '…'


def _how_it_failed(result: ExecResult) -> str:
    if result.timed_out:
        return f'{_PROBE_TIMEOUT:g} 秒内没有结束'
    detail = _first_line(ExecResult(None, '', result.stderr, False, False))
    return f'退出 {result.exit_code}' + (f'：{detail}' if detail else '')


_PENDING = 'git 收尾还没做：文件停在这里，说明 run 在收尾之前被打断了。'
_NOT_WRITTEN = '交接文件没写成，宿主没有做 git 收尾：提交与推送由人来做。'
_T = TypeVar('_T')
_INTERRUPTED_RUN = '被中断（Ctrl-C）：run 没有跑完，Agent 的回答可能只做了一半。'
_INTERRUPTED_AFTER_RUN = (
    '被中断（Ctrl-C）：run 已经做完、回答完整，按键落在之后，复核与 git 收尾没有做。'
)
_INTERRUPTED_VERIFY = '被中断（Ctrl-C）：宿主复核没有跑完。'
_INTERRUPTED_NO_GIT = '被中断（Ctrl-C），不做 git 收尾：工作区里的改动还在，没有提交。'
_INTERRUPTED_GIT = (
    '被中断（Ctrl-C）：git 收尾停在提问或提交的中途，磁盘上的交接文件停在 `git: pending`。先用 '
    '`git status` 与 `git log -1` 看清楚有没有提交，再决定怎么做。'
)


class _Lines:
    """宿主读人的答复（Agent 的提问、git 收尾的选择）的唯一入口。

    readline 在守护线程里跑，停不下来：等它的人走了（Ctrl-C、问题过期），它还卡着等下一行，同一个
    流上后来的 readline 都排在它后面，人敲的下一行被它读走。所以同一时刻至多一个 readline，读到的
    行记下到达时间排队；每次等只收开始等之后到达的行，早于它的是答给已经不等了的那个问题的。
    不用默认线程池：Runner 关闭与解释器退出时都要等池里的线程结束，宿主于是一直等人敲回车。
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._queue: deque[tuple[float, str, Exception | None]] = deque()
        self._reading = False
        self._wakeups: list[Callable[[], None]] = []

    def fileno(self) -> int:
        return self._stream.fileno()

    def readline(self) -> str:
        """同步地等下一行（git 收尾的提问）。等待收得到信号：Ctrl-C 处理抛出的 KeyboardInterrupt
        从这里出去，那次 readline 读到的行留给下一次等。"""
        since = time.monotonic()
        with self._lock:
            while (taken := self._take(since)) is None:
                self._arrived.wait()
        return _line_of(taken)

    async def next_line(self) -> str:
        """在事件循环里等下一行（Agent 的提问）。被取消时那次 readline 照样跑完。"""
        since = time.monotonic()
        loop = asyncio.get_running_loop()
        while True:
            woken: asyncio.Future[None] = loop.create_future()
            with self._lock:
                taken = self._take(since)
                if taken is None:
                    self._wakeups.append(partial(_wake, loop, woken))
            if taken is not None:
                return _line_of(taken)
            await woken

    def _take(self, since: float) -> tuple[str, Exception | None] | None:
        """持锁调用：交回 `since` 之后到达的第一行，早于它的丢掉；还没有就保证有一个 readline
        在跑。"""
        while self._queue:
            arrived, line, error = self._queue.popleft()
            if arrived >= since:
                return line, error
        if not self._reading:
            self._reading = True
            threading.Thread(target=self._read, name='syndica-stdin', daemon=True).start()
        return None

    def _read(self) -> None:
        error: Exception | None = None
        try:
            line = self._stream.readline()
        except Exception as exc:
            line, error = '', exc
        with self._lock:
            self._queue.append((time.monotonic(), line, error))
            self._reading = False
            wakeups, self._wakeups = self._wakeups, []
            self._arrived.notify_all()
        for wake in wakeups:
            wake()


def _line_of(taken: tuple[str, Exception | None]) -> str:
    line, error = taken
    if error is not None:
        raise error
    return line


def _wake(loop: asyncio.AbstractEventLoop, woken: asyncio.Future[None]) -> None:
    def settle() -> None:
        if not woken.done():
            woken.set_result(None)

    try:
        loop.call_soon_threadsafe(settle)
    except RuntimeError:
        # 事件循环已经关了：没有人在等这一行。
        pass


class _CtrlC:
    """`run --handoff` 期间的 SIGINT 处理，取代 `asyncio.Runner` 的那一个，按阶段处理每一次按键。

    Runner 的做法是取消主任务：落在同步代码里时只留下一个挂着的取消，Python 3.11 撤不掉它，协程一
    返回写好的结论就被丢掉、换成 KeyboardInterrupt；第二次按键直接抛出，交接文件写不成。所以宿主
    自己按阶段处理：
    - 干活（run、复核，经 `_interruptible`）：取消干活的那个子任务，主任务从不被取消；
    - 收尾（快照、写交接文件）：只记下按过，交接文件照写，不做 git 收尾；
    - git 收尾（`raising`）：进来之前按过就不开始；开始之后第一次按键当场抛 KeyboardInterrupt，提问
      的等待才停得下。之后的按键只提示一次：停钩子、撤回暂存都有时限，打断它们只会留下半个暂存区；
    - 结论定了之后（git 收尾结束、`settle`）：按键什么都不改，打印的交接文件与退出码一致。
    结论定下之前、git 收尾之外，按到第三次强制退出（抛 KeyboardInterrupt）：收尾卡住时人还有路可走。
    """

    _FORCE = 3

    def __init__(self, warn: Callable[[str], None]) -> None:
        self._warn = warn
        self._loop: asyncio.AbstractEventLoop | None = None
        self._raising = False
        self._unwinding = 0
        self._settled = False
        self.presses = 0
        self.target: asyncio.Future[Any] | None = None

    @property
    def pressed(self) -> bool:
        return self.presses > 0

    def __call__(self, signum: int, frame: object) -> None:
        self.presses += 1
        if self._settled:
            return
        if self._raising:
            self._unwinding += 1
            if self._unwinding == 1:
                raise KeyboardInterrupt
            if self._unwinding == 2:
                self._say(
                    'git 收尾正在停下（停钩子、撤回为提交暂存的新文件），都有时限，等它做完。'
                )
            return
        if self.presses >= self._FORCE:
            raise KeyboardInterrupt
        if self.presses == 1:
            self._say(
                '被中断（Ctrl-C）：正在停下 Agent、写交接文件，不做 git 收尾。再按两次强制退出。'
            )
        else:
            self._say('还在收尾：再按一次强制退出，交接文件可能写不成或停在 git: pending。')
        target = self.target
        if target is not None and not target.done():
            target.cancel()
            if self._loop is not None:
                # 事件循环可能正堵在 select 上：叫醒它，取消才送得进去。
                self._loop.call_soon_threadsafe(lambda: None)

    def _say(self, text: str) -> None:
        try:
            self._warn(text)
        except RuntimeError:
            # 主线程正往同一个流里写时，信号处理再写会报 reentrant call：丢掉这一句，按键照常处理。
            pass

    @contextmanager
    def installed(self) -> Generator[None, None, None]:
        """装上这个处理，结束时换回原来的。继承来的「忽略 SIGINT」照旧忽略；不在主线程时什么都不装
        （只有主线程装得了信号处理）。结论定下之后结束的，不在这里换回：宿主交回结论到 CLI 把它
        打印出来之间的按键还该什么都不改——宿主关闭时 `release`。"""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = signal.getsignal(signal.SIGINT)
        if previous is signal.SIG_IGN:
            yield
            return
        self._loop = asyncio.get_running_loop()
        signal.signal(signal.SIGINT, self)
        try:
            yield
        finally:
            if not self._settled:
                signal.signal(signal.SIGINT, signal.SIG_DFL if previous is None else previous)

    def release(self, handler: Any) -> None:
        """结论定下之后还装着的这个处理换成 `handler`。`handler` 是宿主进这次请求之前的那一个：
        `installed` 看到的「原来的」是 `asyncio.Runner` 为这次 `run` 装的，它绑着一个已经结束的
        任务。"""
        if (
            threading.current_thread() is threading.main_thread()
            and signal.getsignal(signal.SIGINT) is self
        ):
            signal.signal(signal.SIGINT, signal.SIG_DFL if handler is None else handler)

    @contextmanager
    def raising(self) -> Generator[bool, None, None]:
        """git 收尾这一段：交回 False 表示进来之前已经按过，不要开始。开始之后第一次按键当场抛
        KeyboardInterrupt。这一段结束，结论就定了。"""
        self._raising = True
        try:
            started = not self.pressed
            if not started:
                self._raising = False
            yield started
        finally:
            # 先定结论再放下 `_raising`：两句之间也收得到信号，反过来的话那一刻按到第三次就强制
            # 退出，交接文件打印到一半。
            self._settled = True
            self._raising = False

    def settle(self) -> None:
        self._settled = True


def _host_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    loop.set_default_executor(DaemonExecutor())
    return loop


async def _despite_cancellation(work: Coroutine[Any, Any, _T], *, absorb: bool) -> tuple[_T, bool]:
    """跑完 `work` 再理会取消，交回结果与期间有没有被取消。trace 的收尾等着写入任务排空，`run
    --handoff` 期间的第二次 Ctrl-C 只取消、不强制退出：收尾被它打断，trace 就没有结局，交接文件里也
    没有它的目录。`absorb` 时取消到此为止：run 已经做完，收尾时按的键不该把一次做完的 run 换成被中断
    的、把结论与 trace 目录丢掉；不 `absorb` 时跑完之后照样抛出。"""
    task = asyncio.ensure_future(work)
    cancelled = 0
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled += 1
        except BaseException:
            break
    if cancelled and not absorb:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    current = asyncio.current_task()
    if current is not None:
        # 吞下的取消要还回去：不还，这个任务之后的 `asyncio.timeout` 会把自己的到期当成外面的取消。
        for _ in range(cancelled):
            current.uncancel()
    return task.result(), cancelled > 0


async def _interruptible(ctrl_c: _CtrlC, work: Coroutine[Any, Any, _T]) -> _T:
    """把这一段放进一个子任务里跑：Ctrl-C 取消的是它，不是主任务。已经按过就直接取消。"""
    task = asyncio.ensure_future(work)
    ctrl_c.target = task
    if ctrl_c.pressed:
        task.cancel()
    try:
        return await task
    finally:
        ctrl_c.target = None


class _HandoffFailure(Exception):
    """`run --handoff` 没能交出交接文件时给 CLI 的三件套：原错误里的文字经过转义与脱敏——这时两条
    流上的脱敏写入器已经摘掉了。"""

    def __init__(self, *, expected: str, received: str, next_action: str) -> None:
        self.expected = expected
        self.received = received
        self.next_action = next_action
        super().__init__(f'期望: {expected}\n实际: {received}\n下一步: {next_action}')


def _handoff_failure(exc: Exception, redactor: Redactor) -> _HandoffFailure:
    def clean(text: str) -> str:
        return redactor.redact(terminal_safe(text))

    parts = [getattr(exc, name, None) for name in ('expected', 'received', 'next_action')]
    if all(isinstance(part, str) and part.strip() for part in parts):
        expected, received, next_action = cast(list[str], parts)
        return _HandoffFailure(
            expected=clean(expected), received=clean(received), next_action=clean(next_action)
        )
    return _HandoffFailure(
        expected='`run --handoff` 交出交接文件',
        received=clean(f'{type(exc).__name__}: {exc}'),
        next_action='按上面的原因修好之后重跑',
    )


class _TerminalLogHandler(logging.Handler):
    """run 期间 WARNING 以上的日志：转义之后写到宿主当前的 stderr（`--handoff` 下就是脱敏写入器）。
    不装它，日志经 `logging.lastResort` 直接写进进程的 stderr，绕过转义与脱敏，而 Reviewer 指纹的
    告警会带着 Agent 起的文件名。"""

    def __init__(self, stream: Callable[[], TextIO]) -> None:
        super().__init__(logging.WARNING)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = terminal_safe(self.format(record))
            stream = self._stream()
            stream.write(f'{text}\n')
            stream.flush()
        except Exception:
            self.handleError(record)


@dataclass(frozen=True)
class _PendingExport:
    """一个记完、等着在命令结束时导出的 trace（`complete` 为假时不导出）。"""

    store: FileTraceStore
    trace_id: str
    error: BaseException | None
    redactor: Redactor
    complete: bool


@dataclass
class _ChatSession:
    config: str
    runtime: Runtime
    workspace: WorkspaceRouter
    environment: AgentEnvironment
    agent_id: str
    subscription: Subscription
    pump: asyncio.Task[None]
    recording: Recording
    redactor: Redactor
    # 会话锁：持 flock 挡住另一个进程同时续 / 跑这份会话（B-01）。会存盘的会话根都持它、close 时放。
    lock: SessionLock | None = None
    # 最近一轮怎么结束的：chat 在一轮失败时就结束，会话的 trace 以它收尾。
    failure: Exception | None = None


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
        replayer: Replayer | None = None,
        exporter: TraceExporter | None = None,
        entry_points: EntryPointSource = no_entry_points,
        trust: TrustGate,
        prices: PriceTable | None = None,
    ) -> None:
        self._io = io
        # backend 给的默认价格（组合根交进来：surface 不 import backend）；`.syndica.yaml` 的
        # prices: 排在它前面。
        self._default_prices = prices
        # 读 stdin 的只有一处在读：事件泵答问题与 ask_human 追加预算可能同时要一行。
        self._stdin_lock = asyncio.Lock()
        self._budget_explicit = False
        self._entry_points = entry_points
        self._trust = trust
        self._replayer = replayer
        self._exporter = exporter
        self._exports: list[_PendingExport] = []
        self._lines = _Lines(io.stdin)
        self._model_factory = model_factory
        self._workspace_root = Path(workspace_root)
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._runner = asyncio.Runner(loop_factory=_host_loop)
        self._closed = False
        self._ctrl_c: _CtrlC | None = None
        self._sigint_before: Any = None
        self._pins: PinnedImports | None = None
        self._sessions: dict[str, _ChatSession] = {}
        self._announced: set[tuple[str, str]] = set()
        # `--handoff` 下 stdout 只有交接文件，问人的提示改走 stderr。
        self._prompts_to_stderr = False

    def trace(self, request: TraceRequest) -> str | CliOutcome:
        """`syndica trace ...`：读 `.syndica/traces`（与记录同样核对；还没有时当作空的，不建）。
        trace 目录是 Agent 改得到的，store 的告警会带着里面的名字：日志一律转义后进 stderr。"""
        with self._logging_to_stderr():
            try:
                store = FileTraceStore(trace_root(self._workspace_root))
            except HandoffWriteError as error:
                raise TraceUnusable(
                    expected=error.expected, received=error.received, next_action=error.next_action
                ) from error
            if request.action == 'list':
                return self._runner.run(trace_list(store))
            if request.action == 'show':
                return self._runner.run(trace_show(store, request.trace_id))
            if request.action == 'prune':
                return self._runner.run(trace_prune(store, request.keep, request.names))
            if request.action == 'export':
                return self._trace_export(request, store)
            return self._trace_replay(request, store)

    def sessions(self, request: SessionsRequest) -> str | CliOutcome:
        """`syndica sessions list/show/rm`：读 / 删 `.syndica/sessions`。目录是 Agent 改得到的，告警
        转义后进 stderr。`resume` 不走这里——它是交互 chat，由 main 路由进 `_chat`。"""
        with self._logging_to_stderr():
            root = session_root(self._workspace_root)
            store = FileSessionStore(root)
            if request.action == 'list':
                return self._runner.run(_sessions_list(store))
            name = request.name
            if name is None:
                raise AssertionError('sessions show/rm 没有 name')
            if request.action == 'show':
                return self._runner.run(_sessions_show(store, name))
            if request.action == 'rm':
                kept = rubric_root(self._workspace_root)
                rubric_store = FileRubricStore(kept) if kept.is_dir() else None
                return self._runner.run(_sessions_rm(store, root, name, rubric_store))
            rubrics = FileRubricStore(rubric_root(self._workspace_root))
            if request.action == 'rubrics':
                return self._runner.run(_session_rubrics(rubrics, name))
            if request.action == 'activate':
                if request.rubric is None:
                    raise AssertionError('sessions activate 没有 rubric')
                return self._runner.run(_session_activate(rubrics, name, request.rubric))
            raise AssertionError(f'sessions {request.action} 不该路由到 host.sessions')

    def memory(self, request: MemoryRequest) -> str | CliOutcome:
        """`syndica memory list | forget`：读 / 改 `.syndica/memory`。CLI 的工作区层以工作区根为键，
        与 `memory` Module 写下的那一层是同一个。"""
        with self._logging_to_stderr():
            store = FileMemoryStore(memory_root(self._workspace_root))
            scope = MemoryScope.GLOBAL if request.global_scope else MemoryScope.WORKSPACE
            workspace = None if request.global_scope else str(self._workspace_root)
            if request.action == 'list':
                return self._runner.run(_memory_list(store, scope, workspace))
            redactor = host_redactor(self._environ, self._config(request.config).redaction_rules)
            return self._runner.run(_memory_forget(store, scope, workspace, request, redactor))

    def _trace_export(self, request: TraceRequest, store: FileTraceStore) -> CliOutcome:
        """`--otlp` 给的端点不带环境里的头；没给时用环境里的端点与头（与 run 结束时的导出同一套
        规则）。"""
        if self._exporter is None:
            raise TraceUnusable(
                expected='这个入口接上了 OTLP 导出（syndica 的命令行入口会接上）',
                received='宿主建出来时没有给 exporter',
                next_action='从 `syndica` 命令行运行 trace export',
            )
        try:
            resource = environment_resource(self._environ)
            if request.otlp is not None:
                target = command_line_target(request.otlp)
            else:
                found = environment_target(self._environ)
                if found is None:
                    raise ConfigError(
                        expected='trace export 有一个端点：--otlp <url>，或环境里的 '
                        'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT',
                        received='两样都没有',
                        next_action='加上 --otlp http://localhost:4318 这样的端点再试',
                    )
                target = found
        except ConfigError as error:
            raise TraceUnusable(
                expected=error.expected, received=error.received, next_action=error.next_action
            ) from error
        try:
            config = self._config(request.config)
        except ConfigError as error:
            raise TraceUnusable(
                expected='配置读得进来：导出要用它的脱敏规则，决定哪些东西能发出去',
                received=one_line(f'{error.expected}；{error.received}'),
                next_action='修好配置再导出（' + one_line(error.next_action) + '）',
            ) from error
        redactor = host_redactor(self._environ, config.redaction_rules)
        if target.note is not None:
            self._warn(redactor.redact(target.note))
        return self._runner.run(
            trace_export(
                store,
                request.trace_id,
                target=target,
                resource=resource,
                exporter=self._exporter,
                redactor=redactor,
            )
        )

    def _trace_replay(self, request: TraceRequest, store: FileTraceStore) -> CliOutcome:
        """回放一个 trace，回放本身按 structure 记成一个指向原 trace 的新 trace（配置关掉记录时
        不记）。回放自己建事件循环（记录器的写入任务跟着它），所以不在宿主的 runner 里跑。"""
        if self._replayer is None:
            raise TraceUnusable(
                expected='这个入口接上了回放（syndica 的命令行入口会接上）',
                received='宿主建出来时没有给 replayer',
                next_action='从 `syndica` 命令行运行 trace replay',
            )
        header, text = self._runner.run(unusable_replay(store, request.trace_id))
        try:
            config = self._config(request.config)
        except ConfigError as error:
            # 回放比的是记录下的东西，用不着配置；把它读坏的往往正是要回放的那次 run 里的 Agent。
            redactor = host_redactor(self._environ, header.redaction.rules)
            self._warn(
                redactor.redact(
                    f'配置读不了（{one_line(error.received)}）：回放本身的 trace'
                    ' 用这个 trace 记下的脱敏规则，回放照常进行。'
                )
            )
            configured = None
        else:
            redactor = host_redactor(self._environ, config.redaction_rules)
            configured = config.trace_level
        recorder = None
        if trace_level(None, configured, chat=False, warn=self._warn) is not TraceLevel.OFF:
            try:
                trace_directory(self._workspace_root)
            except (HandoffWriteError, OSError) as error:
                self._warn(
                    redactor.redact(f'这次回放不记录 trace：{described(error)}。回放照常进行。')
                )
            else:
                recorder = TraceRecorder(
                    store,
                    redactor,
                    level=TraceLevel.STRUCTURE,
                    source=trace_source(
                        request.argv,
                        self._config_file(request.config),
                        replay_of=header.trace_id,
                        git_commit=self._head_commit(),
                    ),
                )
        try:
            # 回放出原始 run 的失败是预期之内的：Runtime 为它记的 ERROR（带 traceback）不打出来，
            # 结论里的分叉已经说清楚了两边一不一样。
            with _CaptureRuntimeErrors():
                return trace_replay(
                    header,
                    text,
                    replayer=self._replayer,
                    allow_divergence=request.allow_divergence,
                    recorder=recorder,
                    config_file=self._config_file(request.config),
                )
        finally:
            if recorder is not None:
                self._runner.run(self._show_trace(store, recorder.trace_id, redactor))

    async def _show_trace(self, store: FileTraceStore, trace_id: str, redactor: Redactor) -> None:
        """在 stderr 打出一个记完的 trace 的目录与它完不完整；目录不在（没开始记）时不说。"""
        directory = await store.directory(trace_id)
        if directory is None:
            return
        header = await store.get(trace_id)
        label = 'trace' if header is not None and header.status is not None else _INCOMPLETE_TRACE
        self._warn(redactor.redact(f'{label}：{self._shown(directory)}'))

    def _shown(self, directory: Path) -> str:
        """trace 目录相对工作区根；不在工作区里时给绝对路径。"""
        return (
            str(directory.relative_to(self._workspace_root))
            if directory.is_relative_to(self._workspace_root)
            else str(directory)
        )

    def run_request(self, request: RunRequest) -> str | CliOutcome:
        flags = _validated_request(request)
        if request.session is not None:
            if request.handoff:
                raise _config_error(
                    '--handoff 用于一次新的 run',
                    f'同时收到 session={request.session!r}',
                    '去掉 --session：交接要在 run 开始前取快照、在它结束后复核，会话里的 Agent '
                    '早已开始',
                )
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
                    ('trust_module_output', request.trust_module_output),
                    ('trace', request.trace),
                )
                if value is not None and value != ()
            ]
            if overrides:
                raise _config_error(
                    '活动 session 沿用 spawn 时的 Agent、预算与 Flag',
                    f'这些参数会被忽略：{overrides!r}',
                    '删除这些参数；要改变装配请退出 chat，再创建一个新会话',
                )
            with self._logging_to_stderr():
                return self._runner.run(self._run_chat_request(request))
        with self._logging_to_stderr():
            if request.handoff:
                self._sigint_before = signal.getsignal(signal.SIGINT)
                return self._runner.run(self._run_handoff(request, flags))
            return self._runner.run(self._run_request(request, flags))

    @contextmanager
    def _logging_to_stderr(self) -> Generator[None, None, None]:
        handler = _TerminalLogHandler(lambda: self._io.stderr)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            yield
        finally:
            root.removeHandler(handler)

    def _configured_modules(
        self, config: CliConfig, environment: Mapping[str, str], *, goal: str | None = None
    ) -> tuple[RuntimeModule, ...]:
        """配置里启用的 Module，按写的次序。第三方的在这里才 import：调用方拿到的 config 已经过了
        信任确认。`goal` 是 `--goal`，交给验收 Module。"""
        discovered = discover(self._entry_points)
        names = validated_modules(config.modules, discovered)
        if goal is not None and 'acceptance' not in names:
            raise _config_error(
                '--goal 与 modules 里的 acceptance 一起用：Goal 是验收对照的权威',
                f'modules 是 {list(names)!r}，没有 acceptance',
                '在配置的 modules 里加上 acceptance（并按需打开 acceptance_* Flag），或去掉 --goal',
            )
        if 'long-horizon' in names and 'acceptance' not in names:
            raise _config_error(
                'modules 里的 long-horizon 与 acceptance 一起启用：长程的进度只来自验收',
                f'modules 是 {list(names)!r}，没有 acceptance',
                '在 modules 里加上 acceptance 并打开 acceptance_outcome，或去掉 long-horizon',
            )
        entries = {entry.name: entry for entry in discovered}
        redactor = host_redactor(self._environ, config.redaction_rules)
        host = ModuleHost(
            workspace_root=self._workspace_root, environment=environment, redactor=redactor
        )
        built = {
            name: self._builtin_module(name, redactor, goal)
            for name in names
            if name in OPTIONAL_BUILTINS and name != 'long-horizon'
        }
        if 'long-horizon' in names:
            # 长程读的是同一个验收 Module 的 Goal、Rubric 与 L3：另建一个就读不到这次 run 的。
            built['long-horizon'] = LongHorizonModule(cast(AcceptanceModule, built['acceptance']))
        return tuple(
            built[name] if name in built else load_module(entries[name], host, redactor)
            for name in names
        )

    def _builtin_module(
        self, name: str, redactor: Redactor, goal: str | None = None
    ) -> RuntimeModule:
        """可选内置 Module 的「跑时」那一份：记忆要带工作区与 FileMemoryStore、验收要带
        FileRubricStore（都跨会话持久化）与 `--goal`，其余与参考文档那份相同。脱敏器用宿主这份（含
        用户规则），落盘前逐叶脱敏。"""
        if name == 'memory':
            return MemoryModule(
                workspace=str(self._workspace_root),
                store=FileMemoryStore(memory_root(self._workspace_root)),
                redactor=redactor,
            )
        if name == 'acceptance':
            return AcceptanceModule(
                store=FileRubricStore(rubric_root(self._workspace_root)),
                redactor=redactor,
                goal=goal,
            )
        return _optional_builtin(name)

    async def _inspect_artifacts(self, request: InspectRequest) -> str:
        """`artifacts list | show <id>`：工作区的产物索引（§7.4）。路径与 producer 是 Agent 选的，
        转义后再打印。"""
        root = artifact_root(self._workspace_root)
        if not root.is_dir():
            raise _config_error(
                '这个工作区有产物索引（.syndica/artifacts）',
                '还没有：只有装了 acceptance 的 run 才登记产物',
                '在配置的 modules 里启用 acceptance 再跑一次',
            )
        store = FileArtifactStore(root)
        if request.action == 'list':
            lines: list[str] = []
            for ref in await store.list():
                versions = await store.versions_of(ref.id)
                role = '交付物' if ref.is_deliverable else '中间产物'
                lines.append(
                    f'{_rubric_line(ref.id)}\t{role}\t{_rubric_line(ref.kind)}\t'
                    f'{ref.version[:19]}\t{len(versions)} 个版本\t{_rubric_line(ref.producer)}'
                )
            return '\n'.join(lines) if lines else '（还没有登记过产物）'
        name = request.name
        if name is None:
            raise AssertionError('artifacts show 没有 name')
        versions = await store.versions_of(name)
        if not versions:
            raise _config_error(
                f'artifacts show 指向一个登记过的产物 id（{_rubric_line(name)!r}）',
                '索引里没有它',
                '用 syndica artifacts list 看有哪些 id（形如 file:report.md）',
            )
        current = await store.get(name)
        role = '交付物' if current is not None and current.is_deliverable else '中间产物'
        lines = [f'{_rubric_line(name)}  {role}  {_rubric_line(versions[-1].path)}']
        lines.extend(
            f'  {position}. {ref.version}  {_rubric_line(ref.producer)}'
            for position, ref in enumerate(versions, start=1)
        )
        return '\n'.join(lines)

    def _artifact_index(self, modules: Sequence[RuntimeModule]) -> FileArtifactStore | None:
        """装了验收 Module 才建产物索引：L1 靠登记触发，交付物标记也存在这里；别的 run 没有读者。"""
        if not any(module.name == 'acceptance' for module in modules):
            return None
        return FileArtifactStore(artifact_root(self._workspace_root))

    def _module_records(
        self, modules: Sequence[RuntimeModule], config: CliConfig
    ) -> list[dict[str, str]]:
        """第三方 Module 的记录：回放按装着的那一份的发行版名与版本判能不能重建它（D13）。"""
        entries = {entry.name: entry for entry in discover(self._entry_points)}
        third = set(third_party(config.modules))
        records: list[dict[str, str]] = []
        for module in modules:
            if module.name not in third:
                continue
            record = {'name': module.name, 'module_version': module.version}
            entry = entries.get(module.name)
            if entry is not None and entry.distribution is not None:
                record['distribution'] = entry.distribution
            if entry is not None and entry.version is not None:
                record['distribution_version'] = entry.version
            records.append(record)
        return records

    def _unreplayable_modules(
        self, modules: Sequence[RuntimeModule], config: CliConfig
    ) -> str | None:
        """这些 Module 让 run 回放不了的原因；回放得了是 None。第三方 Module 只要有一点会跑的代码
        （服务、启停钩子、插件工厂、工具工厂）又没声明 `replay_safe`，回放就得在这台机器上再跑一遍
        别人的代码；只声明 Flag 的不挡。Module 名与发行版被脱敏改掉的，回放认不出是哪一个。"""
        if any(module.name == 'acceptance' for module in modules):
            return (
                '验收 Module 的伴生调用（投影、L2 / L3、反馈提议）不在主 Agent 的录制轮次里：'
                '回放会把它们当成主 Agent 的轮次，所以装了 acceptance 的 run 不写回放文档'
            )
        if any(module.name == 'background' for module in modules):
            return (
                '背景化 Module 开着时，后台调用的结果在哪一轮进历史取决于它真实跑了多久：'
                '回放复现不了那个先后，所以装了 background 的 run 不写回放文档'
            )
        third = set(third_party(config.modules))
        blocking = [
            module.name
            for module in modules
            if module.name in third and not module.replay_safe and runs_code(module)
        ]
        if blocking:
            return (
                f'第三方 Module {", ".join(blocking)} 有会跑的代码、又没声明 replay_safe：'
                '回放不能在这台机器上再跑一遍它们的代码'
                '（要回放，请作者确认不做 I/O 之后声明 replay_safe）'
            )
        redactor = host_redactor(self._environ, config.redaction_rules)
        named = [
            value for record in self._module_records(modules, config) for value in record.values()
        ]
        changed = sorted({value for value in named if redactor.redact(value) != value})
        if changed:
            return (
                f'脱敏规则会改掉 Module 的名字或发行版 {redactor.redact(repr(changed))}：'
                '回放认不出该重建哪一个（收窄认得它们的那条用户规则）'
            )
        return None

    def _untrusted_output(self, config: CliConfig, trusted: Sequence[str]) -> tuple[str, ...]:
        """结果要收紧的第三方 Module：启用的第三方 Module 里，用户没在命令行上放开的那些。"""
        enabled = third_party(config.modules)
        unknown = [name for name in trusted if name not in enabled]
        if unknown:
            raise _config_error(
                f'--trust-module-output 点名的是启用了的第三方 Module：{list(enabled)!r}',
                f'收到 {unknown!r}',
                '照 .syndica.yaml 的 modules 逐字写第三方 Module 的名字；'
                '内置的 Module 本来就不收紧',
            )
        return tuple(name for name in enabled if name not in trusted)

    def _inspect_modules(self, request: InspectRequest) -> str:
        config = self._config(request.config)
        discovered = discover(self._entry_points)
        if request.action == 'list':
            inside = {
                entry.name: '（代码在工作区之内，Agent 写得到）'
                for entry in discovered
                if entry.problem is None and self._code_inside(entry.target)
            }
            return '\n'.join(
                '\t'.join(
                    one_line(part)
                    for part in (row.name, row.source + inside.get(row.name, ''), row.status)
                )
                for row in module_rows(config.modules, discovered)
            )
        builtins = {module.name: module for module in self._builtin_modules()}
        name = cast(str, request.name)
        module = builtins.get(name)
        if module is None:
            entry = next((entry for entry in discovered if entry.name == name), None)
            if entry is None or entry.problem is not None:
                raise _config_error(
                    f'Module 是内置的（{sorted(builtins)!r}）'
                    '或 modules list 里装得上的第三方 Module',
                    f'收到 {request.name!r}'
                    + ('' if entry is None else f'，它装不上：{entry.problem}'),
                    '运行 syndica modules list 查看全部 Module 与它们的状态',
                )
            # 点名要看才 import：这是用户在命令行上的请求，不是配置替它选的代码。代码在工作区之内
            # 的不算——那是 Agent 写得到的代码，与配置里的代码一样先过信任确认。
            if self._code_inside(entry.target):
                _require_enabled(name, config)
                config = self._trusted_config(request.config)
                _require_enabled(name, config)
            module = self._loaded(config, (name,))[0]
        return terminal_safe(json.dumps(_manifest(module), ensure_ascii=False, indent=2))

    def _code_inside(self, target: str) -> bool:
        """入口点的代码在不在工作区之内；认不出位置的按在里面算。"""
        try:
            return code_location(target, self._workspace_root).inside
        except ConfigError:
            return True

    def _loaded(self, config: CliConfig, names: Sequence[str]) -> tuple[RuntimeModule, ...]:
        """import 这几个第三方 Module，交给它们一份与 run 相同的白名单环境（用完即删）。"""
        discovered = {entry.name: entry for entry in discover(self._entry_points)}
        environment = agent_environment(
            self._environ, config.workspace.pass_env, workspace_root=self._workspace_root
        )
        try:
            redactor = host_redactor(self._environ, config.redaction_rules)
            host = ModuleHost(
                workspace_root=self._workspace_root,
                environment=environment.values,
                redactor=redactor,
            )
            return tuple(load_module(discovered[name], host, redactor) for name in names)
        finally:
            environment.remove()

    def _inspect_flags(self, request: InspectRequest) -> str:
        """这份配置会装的 Module 声明的 Flag。配置启用了第三方 Module 时先过信任确认：要 import
        它们才知道它们声明了什么。"""
        config = self._config(request.config)
        discovered = discover(self._entry_points)
        validated_modules(config.modules, discovered)
        if third_party(config.modules):
            config = self._trusted_config(request.config)
        names = validated_modules(config.modules, discovered)
        third = third_party(names)
        loaded = dict(zip(third, self._loaded(config, third), strict=True))
        modules = (
            *_modules(),
            *(
                _optional_builtin(name) if name in OPTIONAL_BUILTINS else loaded[name]
                for name in names
            ),
        )
        rows = flag_reference(modules)
        if request.action == 'list':
            return flag_lines(rows)
        row = next((row for row in rows if row.key == request.name), None)
        if row is None:
            raise _config_error(
                f'Flag 是这份配置会装的 Module 声明的之一：{[row.key for row in rows]!r}',
                f'收到 {request.name!r}',
                '运行 syndica flags list，再逐字使用其中一个 key；第三方 Module 的 Flag 要它在 '
                'modules 里启用',
            )
        resolved = resolved_flags(
            config.flags, {}, (item for module in modules for item in module.flags)
        )
        source = 'flags:' if row.key in resolved else '缺省'
        effective = resolved.get(row.key, row.default)
        return terminal_safe(
            json.dumps(flag_details(row, effective, source), ensure_ascii=False, indent=2)
        )

    def _builtin_modules(self) -> tuple[RuntimeModule, ...]:
        return (
            *_modules(),
            *(_optional_builtin(name) for name in OPTIONAL_BUILTINS),
            _read_only_manifest(),
            DevModule(self._workspace_root, self._environ),
        )

    def inspect(self, request: InspectRequest) -> str:
        if request.resource == 'artifacts':
            return self._runner.run(self._inspect_artifacts(request))
        if request.resource == 'modules':
            return self._inspect_modules(request)
        if request.resource == 'flags':
            return self._inspect_flags(request)
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
                        'isolation': slot.isolation,
                    }
                    for slot in topology.slots
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    def doctor(self, config: str, *, verify: bool = False) -> CliOutcome:
        return self._runner.run(self._doctor(config, verify=verify))

    def open_chat(
        self,
        config: str,
        max_turns: int | None = None,
        *,
        trace: str | None = None,
        argv: Sequence[str] = (),
        trusted_output: Sequence[str] = (),
        resume: str | None = None,
        goal: str | None = None,
        on_exhausted: str | None = None,
    ) -> str:
        with self._logging_to_stderr():
            return self._runner.run(
                self._open_chat(
                    config,
                    max_turns,
                    trace=trace,
                    argv=argv,
                    trusted_output=trusted_output,
                    resume=resume,
                    goal=goal,
                    on_exhausted=on_exhausted,
                )
            )

    def active_agents(self) -> str:
        return self._runner.run(self._active_agents())

    def close(self, *, interrupted: bool = False) -> None:
        """收掉 chat 会话；`interrupted` 时它们的 trace 以 `cancelled` 收尾。"""
        if self._closed:
            return
        self._closed = True
        if self._pins is not None:
            self._pins.remove()
        if self._ctrl_c is not None:
            self._ctrl_c.release(self._sigint_before)
        pressed = False
        try:
            with self._logging_to_stderr():
                self._runner.run(self._close_sessions(interrupted=interrupted))
                self._flush_exports()
        except KeyboardInterrupt:
            pressed = True
            raise
        finally:
            self._close_loop(pressed=pressed)

    def _close_loop(self, *, pressed: bool = False) -> None:
        """关掉事件循环。这时按的 Ctrl-C、或者收会话时按的那一次（它把循环停在了半路上），会让
        关线程池交出「Event loop stopped before Future completed」这个 RuntimeError 而不是
        KeyboardInterrupt；循环照样关掉了，结论早已打出去，按过键时两者都不再往外抛——收会话时
        按的那一次由 `close` 接着往外抛。没按过键的 RuntimeError 照抛。"""

        def press(signum: int, frame: object) -> None:
            nonlocal pressed
            pressed = True
            raise KeyboardInterrupt

        previous = None
        if (
            threading.current_thread() is threading.main_thread()
            and signal.getsignal(signal.SIGINT) is signal.default_int_handler
        ):
            previous = signal.signal(signal.SIGINT, press)
        try:
            self._runner.close()
        except (KeyboardInterrupt, RuntimeError):
            if not pressed:
                raise
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def _flush_exports(self) -> None:
        """run 结束时的导出放在结论打出去之后：导出可能要等，这时按的 Ctrl-C 不该把一次做完的 run
        变成被中断的。被打断就不再发剩下的，逐个说怎么补发。"""
        pending, self._exports = self._exports, []
        for number, item in enumerate(pending):
            try:
                self._runner.run(self._export(item))
            except KeyboardInterrupt:
                for skipped in pending[number:]:
                    resend = f'syndica trace export {skipped.trace_id[:8]}'
                    self._warn(skipped.redactor.redact(f'导出被中断；要导出就运行 {resend}。'))
                return

    def _config_file(self, path: str) -> Path | None:
        """配置文件本身（trace 记下它的 sha256）；用的是内置默认配置时 None。"""
        file = self._workspace_root / path
        return file if file.is_file() else None

    async def _start_recording(
        self,
        config: CliConfig,
        requested: str | None,
        argv: Sequence[str],
        path: str,
        *,
        chat: bool,
    ) -> tuple[Recording, Redactor]:
        redactor = host_redactor(self._environ, config.redaction_rules)
        level = trace_level(requested, config.trace_level, chat=chat, warn=self._warn)
        recording = await start_recording(
            self._workspace_root,
            level,
            redactor,
            argv=argv,
            config=self._config_file(path),
            warn=self._warn,
            git_commit=None if level is TraceLevel.OFF else self._head_commit(),
            mirror=self._exporter is not None and self._export_wanted(),
        )
        return recording, redactor

    def _export_wanted(self) -> bool:
        """环境里设了能用的 OTLP 端点：这次记录要在内存里留一份，结束时从它导出。"""
        try:
            return environment_target(self._environ) is not None
        except ConfigError:
            return False

    def _head_commit(self) -> str | None:
        """工作区所在仓库的 HEAD（trace 头记下它，说清这个 trace 是在哪个版本上记的）；不在 git 仓库
        里、还没有提交、git 用不了时 None。经宿主 git 环境跑：仓库配置里的 fsmonitor 不会被执行。"""
        try:
            completed = HostGit(self._workspace_root, self._environ).attempt(
                'rev-parse', '--verify', '--quiet', 'HEAD^{commit}', timeout=_HEAD_TIMEOUT
            )
        except GitUnavailable:
            return None
        if completed.returncode != 0:
            return None
        head = completed.stdout.decode('ascii', 'replace').strip()
        return head if _COMMIT_ID.fullmatch(head) else None

    async def _finish_recording(
        self,
        recording: Recording,
        error: BaseException | None,
        redactor: Redactor,
        traces: list[tuple[str, bool]] | None = None,
    ) -> None:
        """收尾并在 stderr 打出 trace 目录（相对工作区根）与它完不完整；`traces` 收下这两样。收尾
        自己出错只告警：它不能顶替 run 的结论。"""
        try:
            finished, pressed = await _despite_cancellation(
                recording.finish(run_status(error)), absorb=error is None
            )
        except Exception as exc:
            self._warn(
                redactor.redact(
                    f'trace 没能收尾：{type(exc).__name__}: {exc}。run 的结论不受影响；这个 trace '
                    '没有结局，不要拿它回放。'
                )
            )
            return
        if pressed:
            self._warn('收尾 trace 时按了 Ctrl-C：run 已经做完，结论照常给出。')
        if finished is None:
            return
        shown = self._shown(finished.directory)
        label = 'trace' if finished.complete else _INCOMPLETE_TRACE
        self._warn(redactor.redact(f'{label}：{shown}'))
        if traces is not None:
            traces.append((shown, finished.complete))
        if recording.store is not None and recording.recorder is not None:
            self._exports.append(
                _PendingExport(
                    recording.store,
                    recording.recorder.trace_id,
                    error,
                    redactor,
                    finished.complete,
                )
            )

    async def _export(self, item: _PendingExport) -> None:
        """环境里设了 OTLP 端点就把这个 trace 发出去。失败只告警：run 的结论不受影响。被中断的 run
        与不完整的 trace 不发：前者人已经按了 Ctrl-C，后者回放与导出都拒绝。"""
        redactor, trace_id = item.redactor, item.trace_id
        try:
            target = environment_target(self._environ)
            resource = environment_resource(self._environ)
        except ConfigError as problem:
            self._warn(
                redactor.redact(f'trace 没有导出：{described(problem)}。run 的结论不受影响。')
            )
            return
        if target is None:
            return
        resend = f'syndica trace export {trace_id[:8]}'
        if run_status(item.error) is SpanStatus.CANCELLED:
            self._warn(redactor.redact(f'被中断的 run 不导出 trace；要导出就运行 {resend}。'))
            return
        if not item.complete:
            self._warn(redactor.redact('不完整的 trace 不导出（原因见上面的 ERROR）。'))
            return
        if target.note is not None:
            self._warn(redactor.redact(target.note))
        if self._exporter is None:
            self._warn(
                redactor.redact('环境里设了 OTLP 端点，但这个宿主没有接上导出器：trace 没有导出。')
            )
            return
        store = item.store.mirror if isinstance(item.store, MirroredTraceStore) else item.store
        try:
            async with asyncio.timeout(_EXPORT_DEADLINE):
                await self._exporter(
                    store,
                    trace_id,
                    url=target.url,
                    headers=target.headers,
                    resource=resource,
                    redactor=redactor,
                )
        except TimeoutError:
            self._warn(
                redactor.redact(
                    f'trace 没有导出完：collector 在 {_EXPORT_DEADLINE:g} 秒里没有收完。'
                    f'run 的结论不受影响；collector 快起来之后运行 {resend} 重发。'
                )
            )
        except Exception as problem:
            # 配置的毛病（没装 extra）重发也没用；collector 已经收下一部分的，重发会让它收到重复的。
            resendable = getattr(problem, 'resendable', not isinstance(problem, ConfigError))
            hint = f'collector 好了之后运行 {resend} 重发。' if resendable is True else ''
            self._warn(
                redactor.redact(f'trace 没有导出：{described(problem)}。run 的结论不受影响。{hint}')
            )

    def _config(self, path: str) -> CliConfig:
        """相对路径按工作区根解析：配置属于工作区，不属于进程碰巧所在的目录（`python -m syndica`
        的工作区就是当前目录，两者一致）。"""
        file = self._workspace_root / path
        if path == '.syndica.yaml' and not file.exists():
            return _default_config(Path(path))
        return load_config(file)

    def _trusted_config(self, path: str) -> CliConfig:
        """run、chat、doctor 用的配置：工作区之内的那一份要先被信任（M4 D15）。工作区之外的是人
        显式指过去的，Agent 碰不到。"""
        file = self._workspace_root / path
        if path == '.syndica.yaml' and not file.exists():
            return _default_config(Path(path))
        if self._trust.store_for is None or not within(file, self._workspace_root):
            return load_config(file)
        store = self._trust_store()
        snapshot = self._snapshot(file)
        config = config_of(snapshot)
        if store.get(snapshot.path) != snapshot.digest:
            self._confirm(snapshot, config)
            store.put(snapshot.path, snapshot.digest)
        digested = [
            source for source in (*snapshot.impls, *snapshot.module_sources) if source.inside
        ]
        if digested:
            # 工作区里种下的 `.pyc` 会被当成源文件的缓存：字节码改放到本进程新建的目录。
            sys.pycache_prefix = private_pycache_prefix()
            if self._pins is not None:
                self._pins.remove()
            self._pins = PinnedImports(digested)
            self._pins.install()
        return config

    def _snapshot(self, file: Path) -> ConfigSnapshot:
        return read_snapshot(yaml_file(file), self._workspace_root, discover(self._entry_points))

    def _trust_store(self) -> TrustStore:
        assert self._trust.store_for is not None
        store = self._trust.store_for(self._environ)
        if store is None:
            raise _config_error(
                '找得到放信任存储的地方',
                'SYNDICA_TRUST_STORE、XDG_CONFIG_HOME 与 HOME 都没有设',
                '设置 XDG_CONFIG_HOME（或 HOME）后重试',
            )
        if within(store.path, self._workspace_root):
            raise _config_error(
                '信任存储在工作区之外',
                f'{one_line(str(store.path))} 在工作区之内，Agent 改得到它',
                '把 XDG_CONFIG_HOME 或 SYNDICA_TRUST_STORE 指到工作区之外',
            )
        return store

    def _confirm(self, snapshot: ConfigSnapshot, config: CliConfig) -> None:
        """问人。只认交互终端：管道里的 `yes` 不算。提示与回答走 stderr，不混进 run 的答案。"""
        shown = one_line(str(snapshot.path))
        if not (self._io.stdin.isatty() and self._io.stderr.isatty()):
            raise _config_error(
                f'工作区之内的配置 {shown} 经人确认之后才用（第一次用或改过之后）',
                f'它没有被信任（摘要 {snapshot.digest}），而 stdin / stderr 不是交互终端',
                f'看过这份配置之后运行 syndica config trust --sha256 {snapshot.digest}，再重跑',
            )
        self._io.stderr.write(describe(snapshot, config) + '\n信任这份配置并继续？[y/N] ')
        self._io.stderr.flush()
        if self._lines.readline().strip().lower() not in {'y', 'yes'}:
            raise _config_error(
                f'确认之后才用 {shown}',
                '回答不是 y',
                '看过之后重跑并回答 y，或运行 syndica config trust',
            )
        again = self._snapshot(snapshot.path)
        if again.digest != snapshot.digest:
            raise _config_error(
                '确认的那一份就是要用的那一份',
                f'确认期间 {shown} 或它引用的文件变了',
                '重跑这条命令，看过新的内容再确认',
            )

    def trust_config(self, config: str, sha256: str | None) -> str:
        """`syndica config trust`：记下这份配置现在的摘要。非交互时必须给 `--sha256`，从拒绝消息里
        抄来的摘要等于人读过。"""
        file = yaml_file(self._workspace_root / config)
        if not within(file, self._workspace_root):
            return f'{one_line(str(file))} 在工作区之外，用它不需要信任确认'
        if self._trust.store_for is None:
            raise _config_error(
                '宿主接着信任存储',
                '这个宿主是不设信任闸构造的',
                '用 syndica 命令行运行 config trust',
            )
        store = self._trust_store()
        snapshot = self._snapshot(file)
        if sha256 is None:
            self._confirm(snapshot, config_of(snapshot))
        elif sha256 != snapshot.digest:
            raise _config_error(
                '--sha256 是这份配置现在的摘要',
                f'给的是 {one_line(sha256)}，现在是 {snapshot.digest}',
                '配置在那之后变了：重新看一遍，再用新的摘要运行 syndica config trust --sha256',
            )
        store.put(snapshot.path, snapshot.digest)
        return f'已信任 {one_line(str(snapshot.path))}（摘要 {snapshot.digest}）'

    def _open_workspace(self, config: CliConfig) -> tuple[WorkspaceRouter, AgentEnvironment]:
        """run、chat、doctor 共用的入口：Agent 在哪条路径上都拿到同一份环境与同一个路由。

        路由总带着文件污点（打开这一刻取基线）；`workspace.versions` 打开时（缺省）再接版本层与
        隔离。git 与临时目录由宿主找好交给内核：内核不读 PATH 与 TMPDIR。"""
        environment = agent_environment(
            self._environ, config.workspace.pass_env, workspace_root=self._workspace_root
        )
        try:
            local = LocalWorkspace(
                str(self._workspace_root),
                shell=_SHELL,
                env=environment.values,
                private=_PRIVATE,
            )
            taint = FileTaint(self._workspace_root)
            if not config.workspace.versions:
                return WorkspaceRouter(local, taint=taint), environment
            git = shutil.which('git', path=self._environ.get('PATH'))
            router = WorkspaceRouter(
                local,
                versions=VersionStore(
                    self._workspace_root,
                    redactor=host_redactor(self._environ, config.redaction_rules),
                ),
                isolator=LocalIsolator(
                    shell=_SHELL,
                    env=environment.values,
                    git=git,
                    scratch=tempfile.gettempdir() if git else None,
                ),
                taint=taint,
            )
        except BaseException:
            environment.remove()
            raise
        return router, environment

    def _prompt(self, text: str) -> None:
        """问人的提示。问题、选项与确认参数来自模型：转义之后才进终端。"""
        stream = self._io.stderr if self._prompts_to_stderr else self._io.stdout
        stream.write(terminal_safe(text))
        stream.flush()

    def _answers(self) -> TextIO | None:
        """git 收尾读答复的流：经 `_Lines`，与 Agent 的提问共用一个读者。没有 stdin（`sys.stdin`
        是 None）时交回 None，`ask` 按「我来处理」算。"""
        return None if cast(object, self._io.stdin) is None else cast(TextIO, self._lines)

    def _warn(self, text: str) -> None:
        """写到 stderr 的告警与运行时错误报告：里面有异常文字与 Agent 起的名字，转义之后才进
        终端。"""
        self._io.stderr.write(f'{terminal_safe(text)}\n')
        self._io.stderr.flush()

    async def _answer(
        self,
        runtime: Runtime,
        context: Context,
        records: list[logging.LogRecord],
        *,
        agent_id: str,
        start: int,
        fail_without_text: bool,
        reports: list[str] | None = None,
    ) -> str:
        """把目标 Agent 的终局交给用户：Runtime 错误写进 stderr，回答作为返回值。`reports` 另外
        收下这些错误报告（交接文件里要列出来）。

        这些错误记录被 `_CaptureRuntimeErrors` 截下、不会冒到 logging；子 Agent 失败时根 Agent 照样
        可能给出回答，不在这里写出去，失败就只剩一个看起来正常的结论。被最终错误引用的那一条不重复写。
        """
        names = {info.agent_id: info.name for info in await runtime.registry.list()}
        failed = context.stop_reason is StopReason.ERROR
        final = _own_failure(records, agent_id) if failed else None
        if failed and final is None and records:
            final = records[-1]
        listed = [record for record in records if record is not final]
        for position, record in enumerate(listed, start=1):
            report = _runtime_error_report(record, names, f'{position}/{len(listed)}')
            self._warn(report)
            if reports is not None:
                reports.append(report)
        if failed:
            cause = _syndica_cause(final) if final is not None else None
            if cause is None:
                raise _error_failure(final)
            # 底层错误自己的三件套就是用户该看的：再套一层「实际：……」，「下一步」就出现两次，
            # 外层那条还是泛泛的「先处理上面的错误」。
            raise _AgentFailure(
                expected=cause.expected,
                received=cause.received,
                next_action=cause.next_action,
                headline=f'Agent {names.get(agent_id, "?")}（{agent_id}）以错误结束：',
            )
        text = last_answer(context, start)
        if text is None:
            missing = _no_answer(context.stop_reason)
            if fail_without_text:
                raise missing
            self._warn(missing.message)
            return ''
        notice = _CUT_SHORT.get(context.stop_reason)
        if notice is not None:
            self._warn(notice)
        view = runtime._budget_of(agent_id)  # pyright: ignore[reportPrivateUsage]
        if view is not None and (self._budget_explicit or context.stop_reason is StopReason.BUDGET):
            self._warn(_budget_line(view, context.stop_reason))
        if context.verdict is not None:
            self._warn(_verdict_line(context.verdict))
        if 'acceptance' in runtime.modules:
            await self._announce_proposals(agent_id)
        return terminal_safe(text)

    async def _announce_proposals(self, agent_id: str) -> None:
        """把这次新提议的验收标准告诉人：提议不会自己生效（§7.7），人不知道就永远停在提议。"""
        root = rubric_root(self._workspace_root)
        if not root.is_dir():
            return
        try:
            rubrics = await FileRubricStore(root).rubrics(agent_id, include_proposed=True)
        except SyndicaError as exc:
            self._warn(f'读不出这次的验收提议：{exc.received}。{exc.next_action}')
            return
        for rubric in rubrics:
            key = (agent_id, rubric.id)
            if rubric.status is not RubricStatus.PROPOSED or key in self._announced:
                continue
            self._announced.add(key)
            self._warn(
                f'提议的验收标准 {_rubric_line(rubric.id)}：{_rubric_line(rubric.statement)}'
                f'（人确认后生效：syndica sessions activate {agent_id} {_rubric_line(rubric.id)}）'
            )

    def _budget_policy(
        self,
        config: CliConfig,
        *,
        turns: int | None,
        tokens: int | None = None,
        cost: Decimal | None = None,
        wall_clock_s: float | None = None,
        on_exhausted: str | None = None,
    ) -> BudgetPolicy:
        """这次 run / chat 的根预算（spec §8.4）：`--max-*` 管整棵派活树，子 Agent 从父的剩余里切。
        价格先查 `.syndica.yaml` 的 prices:，再查 backend 的默认值；设了成本上限时在 stderr 说清价格
        从哪来——项目配置里的 0 价就能让 `--max-cost` 形同虚设，人得看得见。"""
        # 只有人自己设了预算时才在 run 结束时打预算那一行（或者真停在了预算上）：缺省的 200 轮只是树
        # 的规模上限，每次 run 都说一句是噪音。
        self._budget_explicit = any(
            value is not None for value in (turns, tokens, cost, wall_clock_s)
        )
        prices: PriceTable | None = self._default_prices
        if config.prices:
            prices = StaticPrices(dict(config.prices), fallback=self._default_prices)
        if cost is not None:
            listed = ', '.join(sorted(config.prices)) or '无'
            self._warn(
                f'成本上限 {cost}：价格先查 {config.source} 的 prices:（{listed}），'
                '再查 backend 的默认价格（只认得免费模型）。'
            )
        disposition = Exhaustion(on_exhausted or Exhaustion.GRACEFUL_WRAPUP.value)
        return BudgetPolicy(
            root=Budget(
                turns=_DEFAULT_ROOT_TURNS if turns is None else turns,
                tokens=tokens,
                cost=cost,
                wall_clock_s=wall_clock_s,
            ),
            on_exhausted=disposition,
            prices=prices,
            ask_human=self._ask_budget if disposition is Exhaustion.ASK_HUMAN else None,
        )

    async def _ask_budget(self, agent_id: str, view: BudgetView) -> Budget | None:
        """`--on-exhausted ask_human`：问人要不要给这个 Agent 追加轮数。回车或读不到 = 不追加
        （收尾）。"""
        spent = ' / '.join(view.exhausted) or '轮次'
        self._prompt(
            f'Agent {agent_id} 的预算快用完了（{spent}）。再给它几轮？'
            '输入一个正整数，直接回车就让它收尾交付\n> '
        )
        async with self._stdin_lock:
            answer = (await self._lines.next_line()).strip()
        if not answer:
            return None
        try:
            extra = int(answer)
        except ValueError:
            self._warn(f'没认出 {answer!r} 是正整数：按不追加处理，Agent 收尾交付。')
            return None
        if extra <= 0:
            return None
        return Budget(turns=extra)

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
            async with self._stdin_lock:
                answer = await self._lines.next_line()
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

    async def _doctor(self, path: str, *, verify: bool) -> CliOutcome:
        """D16：在 Agent 的环境里自检。git 与 python 两行只是情况说明；verify 用到的可执行文件跑不
        起来、`--verify` 里有命令没过，退出 1。"""
        config = self._trusted_config(path)
        workspace, environment = self._open_workspace(config)
        failed = False
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
                role_policies=role_policies(judge, config.danger_roles),
            )
            git = HostGit(self._workspace_root, self._environ)
            lines = [
                f'config: ok {one_line(config.source)}',
                f'models: ok {", ".join(names)}',
                f'workspace: ok {one_line(str(self._workspace_root))}',
                f'env: {" ".join(environment.values)}',
                _git_line(git),
            ]
            portal = workspace.portal()
            where, found = await _locate(portal, 'python3')
            if where is None:
                lines.append(f'python: 没有（Agent 的环境里找不到 python3{_why_not_found(found)}）')
            else:
                lines.append(f'python: ok（{await self._describe(portal, where)}）')
            executables, unparsed, unchecked = _verify_executables(config.workspace.verify)
            for command, error in unparsed:
                failed = True
                lines.append(
                    f'exe: failed（拆不开 verify 命令 `{one_line(command)}`：{error}）。下一步：'
                    '修好 .syndica.yaml 里这条命令的引号'
                )
            for word in unchecked:
                lines.append(
                    f'exe: unchecked {one_line(word)}（不是单独的程序名，或这条命令自己改了 PATH：'
                    'doctor 不展开它，`doctor --verify` 会真的跑它）'
                )
            for executable in executables:
                where, found = await _locate(portal, executable)
                if where is None:
                    failed = True
                    lines.append(
                        f'exe: failed {one_line(executable)}（Agent 的环境里找不到它'
                        f'{_why_not_found(found)}）。下一步：把它所在的目录加进 PATH（Agent 的环境'
                        '透传宿主的 PATH），或把它需要的环境变量写进 .syndica.yaml 的 '
                        'workspace.pass_env'
                    )
                else:
                    lines.append(
                        f'exe: ok {one_line(executable)}（{await self._describe(portal, where)}）'
                    )
        finally:
            try:
                await workspace.close()
            finally:
                environment.remove()
        if verify:
            if not config.workspace.verify:
                lines.append('verify: not_configured（.syndica.yaml 的 workspace.verify 是空的）')
            before, _ = _changes(git)
            for result in await self._verify(config):
                failed = failed or not result.passed
                lines.append(
                    f'verify: {"passed" if result.passed else "failed"} {one_line(result.command)}'
                    f'（{result.summary}）'
                )
            after, _ = _changes(git)
            touched = _differences(before, after)
            if touched:
                failed = True
                lines.append(
                    f'verify: 改了工作区里的文件：{", ".join(map(one_line, touched))}'
                    '（`run --handoff` 会因此判复核失败）。下一步：让这些命令只读地跑，或把它们'
                    '生成的文件加进 .gitignore'
                )
        redactor = host_redactor(self._environ, config.redaction_rules)
        return CliOutcome(redactor.redact('\n'.join(lines)), 1 if failed else 0)

    async def _describe(self, portal: Portal, where: str) -> str:
        """找到了的程序怎么写进那一行：内建命令不跑；解析到工作区里的不跑（那是项目自己的代码，只有
        `run --handoff` 与 `doctor --verify` 才执行项目代码）；其余跑 `--version`，跑不出来只算
        版本未知，不算失败。"""
        if '/' not in where:
            return 'shell 内建命令'
        if _inside_workspace(where, self._workspace_root):
            return f'在工作区里：{one_line(where)}，doctor 不运行它'
        version = await portal.run(
            f'{shlex.quote(where)} --version', timeout=_PROBE_TIMEOUT, cwd='.'
        )
        if _ran(version):
            return _first_line(version) or one_line(where)
        return f'版本未知：{_how_it_failed(version)}'

    async def _open_chat(
        self,
        path: str,
        max_turns: int | None,
        *,
        trace: str | None,
        argv: Sequence[str],
        trusted_output: Sequence[str],
        resume: str | None = None,
        goal: str | None = None,
        on_exhausted: str | None = None,
    ) -> str:
        config = self._trusted_config(path)
        if resume is not None:
            # 恢复一份不存在的会话要在起 trace 录制 / 开工作区 / 建模型之前就失败（评审 M6）；也把
            # 「没这个会话」与后面「角色没登记」两类原因分开。
            store = FileSessionStore(session_root(self._workspace_root))
            if not await store.exists(resume):
                raise _config_error(
                    'sessions resume 指向 .syndica/sessions 里已存在的会话',
                    f'收到 session={resume!r}；没有这个会话',
                    '用 sessions list 看现有会话 id；id 是根 Agent 的 id',
                )
        catalog = config.catalog
        budget = self._budget_policy(config, turns=max_turns, on_exhausted=on_exhausted)
        recording, redactor = await self._start_recording(config, trace, argv, path, chat=True)
        try:
            workspace, environment = self._open_workspace(config)
        except BaseException as exc:
            await self._finish_recording(recording, exc, redactor)
            raise
        runtime: Runtime | None = None
        subscription: Subscription | None = None
        pump: asyncio.Task[None] | None = None
        session_lock: SessionLock | None = None
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
                role_policies=role_policies(judge, config.danger_roles),
            )
            modules = _agent_modules(
                self._workspace_root,
                self._environ,
                configured=self._configured_modules(config, environment.values, goal=goal),
            )
            # 会话是落盘的 run 产物，和 trace 一样受「关了录制就不往磁盘写」约束。但 resume 是显式续
            # 一份已存的会话，无论录制开没开都要有 SessionStore 才读得到、续得上。
            want_sessions = recording.recorder is not None or resume is not None
            runtime = Runtime(
                agents=factories,
                orchestration=_ORCHESTRATION,
                modules=modules,
                flags=resolved_flags(
                    config.flags, {}, (item for module in modules for item in module.flags)
                ),
                confirmation_handler=MailboxConfirmationHandler(timeout=_CONFIRMATION_TIMEOUT),
                tracer=recording.recorder,
                untrusted_output_modules=self._untrusted_output(config, trusted_output),
                workspace=workspace,
                sessions=(
                    FileSessionStore(session_root(self._workspace_root)) if want_sessions else None
                ),
                redactor=redactor if want_sessions else None,
                artifacts=self._artifact_index(modules),
                budget=budget,
            )
            subscription = runtime.events.subscribe()
            pump = asyncio.create_task(
                self._event_pump(runtime, subscription),
                name='cli-chat-human-events',
            )
            if resume is not None:
                # 先加锁再恢复：挡住另一个进程同时续同一份会话（last-writer 会丢一段工作，B-01）。
                session_lock = acquire_session_lock(session_root(self._workspace_root), resume)
                if not await runtime.ensure_active(resume):
                    raise _config_error(
                        'sessions resume 指向 .syndica/sessions 里已存在的会话',
                        f'收到 session={resume!r}；没有这个会话、或它的角色没登记',
                        '用 sessions list 看现有会话 id；id 是根 Agent 的 id',
                    )
                agent_id = resume
            else:
                factory = runtime.agent_factory('Orchestrator')
                if factory is None:
                    catalog.get('Orchestrator')
                    raise AssertionError(
                        'catalog.get returned without an Orchestrator runtime factory'
                    )
                agent_id = await runtime.spawn(factory, exit_mode=ExitMode.ZOMBIE, session=True)
                # 新建的会话也持锁（sid 刚生成、必不冲突）：第二个进程想续 / 跑这份活会话会被挡住
                # （评审 H2——否则一个在跑的 chat 不持锁，另一端 resume 就把它的轨迹盖掉）。
                if want_sessions:
                    session_lock = acquire_session_lock(
                        session_root(self._workspace_root), agent_id
                    )
            self._sessions[agent_id] = _ChatSession(
                config=path,
                runtime=runtime,
                workspace=workspace,
                environment=environment,
                agent_id=agent_id,
                subscription=subscription,
                pump=pump,
                recording=recording,
                redactor=redactor,
                lock=session_lock,
            )
            return agent_id
        except BaseException as exc:
            try:
                if session_lock is not None:
                    session_lock.release()
                if runtime is not None:
                    await runtime.shutdown()
                if pump is not None:
                    await asyncio.gather(pump, return_exceptions=True)
                if subscription is not None:
                    subscription.close()
                await workspace.close()
            finally:
                environment.remove()
                await self._finish_recording(recording, exc, redactor)
            raise

    async def _run_session(
        self, session: _ChatSession, prompt: str, *, fail_without_text: bool = False
    ) -> str:
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
                agent_id=session.agent_id,
                start=_after_message(context, delivered.message_id),
                fail_without_text=fail_without_text,
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
            try:
                answer = await self._run_session(session, request.prompt)
            except Exception as exc:
                root_session.failure = exc
                raise
            root_session.failure = None
            return answer
        # 走到这里：目标不是本进程里的活动 Agent。只有顶层 `run --session` 才跨进程续一份落盘会话；
        # chat 里的 `:use <id>` 不隐式恢复别的会话（评审 M4）——它只在活动 Agent 之间切。
        if not request.allow_resume:
            raise _config_error(
                'run --session / :use 指向当前 chat 进程里的活动 Agent',
                f'收到 session={target!r}；当前进程没有这个活动目标',
                '输入 :agents 看活动目标；跨进程续一份落盘会话用 `sessions resume <id>`',
            )
        # 不在本进程：跨进程续一份落盘会话，非交互地跑一轮。先只查存在性（不建模型、不开工作区）——
        # session 不存在是一类「无效 run 选项」，要在建任何东西之前失败。存在就 `_open_chat` 续起、
        # `_run_session` 发一条取答复，close 收尾存盘；装配沿用快照、不接受覆盖（已拒）。
        store = FileSessionStore(session_root(self._workspace_root))
        if not await store.exists(target):
            raise _config_error(
                'run --session 指向一份存在的会话'
                '（本进程的活动 Agent，或 .syndica/sessions 里的落盘会话）',
                f'收到 session={target!r}；两处都没有它',
                '用 sessions list 看落盘的会话 id；跨进程交互续跑用 sessions resume <id>',
            )
        agent_id = await self._open_chat(
            request.config,
            None,
            trace=request.trace,
            argv=request.argv,
            trusted_output=(),
            resume=target,
        )
        session = self._sessions[agent_id]
        # 一次性 run：没有文本就是失败（预算耗尽会立刻 MAX_TURNS、无输出），要让它非零退出而不是
        # 静默 exit 0（评审 M5）。失败记在会话上，收尾的 trace 以 error 收口（评审 L1）。
        try:
            answer = await self._run_session(session, request.prompt, fail_without_text=True)
        except Exception as exc:
            session.failure = exc
            raise
        session.failure = None
        return answer

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

    async def _close_sessions(self, *, interrupted: bool = False) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            # 读人类输入的泵失败时，它的异常在那一轮已经交给了调用方；收尾再抛一次会让后面的
            # 工作区与临时目录都不再被收拾。
            try:
                await session.runtime.shutdown()
                # Runtime 关了，泵即使收到答复也送不出去；它可能正等着人敲回车，不取消就一直等。
                session.pump.cancel()
                await asyncio.gather(session.pump, return_exceptions=True)
                session.subscription.close()
                await session.workspace.close()
            finally:
                session.environment.remove()
                if session.lock is not None:
                    session.lock.release()
                await self._finish_recording(
                    session.recording,
                    KeyboardInterrupt() if interrupted else session.failure,
                    session.redactor,
                )

    def _describe_run(
        self,
        tracer: TraceRecorder,
        request: RunRequest,
        config: CliConfig,
        catalog: AgentCatalog,
        factories: Mapping[str, AgentFactory],
        profile: Callable[[Mapping[str, DangerPolicy]], dict[str, object]],
        policy: DangerPolicy,
        prompt: str,
        assembled: tuple[Topology, AgentSlot] | None,
        modules: Sequence[RuntimeModule],
    ) -> None:
        """把回放文档里只有宿主知道的部分交给 recorder。回放造不出来的 run 不交（trace 照常记录），
        只在人点名要了 replay / full 时说一句：默认级别下每次 run 都说同一句是噪音。"""
        explicit = request.trace is not None or config.trace_level is not None
        reason: str | None = None
        roles: dict[str, dict[str, object]] = {}
        role_policies: dict[str, DangerPolicy] = {}
        reason = self._unreplayable_modules(modules, config) or _unreplayable_budget(request)
        if reason is None:
            try:
                roles, role_policies = frozen_roles(catalog, factories, policy)
            except Exception as error:
                # 建不出角色只少回放文档：这次 run 要不要用到那个角色，要等它跑起来才知道。
                reason = f'读冻结之后的角色时出错：{type(error).__name__}: {error}'
                explicit = True
            else:
                custom = sorted(
                    name
                    for name, role in roles.items()
                    if role['impl'] is not None and role['impl'] not in FROZEN_IMPLS
                )
                if custom:
                    reason = (
                        f'角色 {", ".join(custom)} 是自定义的类式角色，回放只重建内核内置的那几个'
                    )
        if reason is not None:
            if explicit:
                self._warn(f'trace 里不写回放文档：{reason}；trace replay 回放不了这次 run。')
            return
        entry: dict[str, object] = (
            {'mode': 'query', 'agent': request.agent or 'Orchestrator'}
            if assembled is None
            else {'mode': 'slot', 'slot': assembled[1].name}
        )
        tracer.describe_run(
            host=profile(role_policies),
            roles=roles,
            entry={**entry, 'prompt': prompt, 'trust': 'trusted'},
            topology=None if assembled is None else assembled[0],
        )

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
        flags: Mapping[str, str],
        *,
        config: CliConfig | None = None,
        logs: list[DevLog] | None = None,
        stops: list[StopReason | None] | None = None,
        reports: list[str] | None = None,
        traces: list[tuple[str, bool]] | None = None,
    ) -> str:
        """一次 run 的回答，记成一个 trace（D15）。`logs` 收下这个 Runtime 的 `dev_log`（run 结束、
        Runtime 关掉之后照样能读），`stops` 收下根 Agent 的结局，`reports` 收下运行中的错误报告，
        `traces` 收下 trace 目录（相对工作区根）与它完不完整。"""
        config = config or self._trusted_config(request.config)
        recording, redactor = await self._start_recording(
            config, request.trace, request.argv, request.config, chat=False
        )
        error: BaseException | None = None
        try:
            return await self._run_traced(
                request,
                flags,
                config,
                recording.recorder,
                logs=logs,
                stops=stops,
                reports=reports,
            )
        except BaseException as exc:
            error = exc
            raise
        finally:
            await self._finish_recording(recording, error, redactor, traces)

    async def _run_traced(
        self,
        request: RunRequest,
        flags: Mapping[str, str],
        config: CliConfig,
        tracer: TraceRecorder | None,
        *,
        logs: list[DevLog] | None,
        stops: list[StopReason | None] | None,
        reports: list[str] | None,
    ) -> str:
        catalog = config.catalog
        prompt = _READ_ONLY_NOTICE + request.prompt if request.no_write else request.prompt
        workspace, environment = self._open_workspace(config)
        tools = local_tools(workspace.portal(), output_limit=_OUTPUT_LIMIT)
        try:
            with _CaptureRuntimeErrors() as runtime_errors:
                # Module 与 Flag 先于模型定下：取值写错要在建任何模型之前就失败。
                modules = _agent_modules(
                    self._workspace_root,
                    self._environ,
                    tools if request.no_write else None,
                    configured=self._configured_modules(
                        config, environment.values, goal=request.goal
                    ),
                )
                resolved = resolved_flags(
                    config.flags, flags, (item for module in modules for item in module.flags)
                )
                untrusted = self._untrusted_output(config, request.trust_module_output)
                assembled = _assembled_topology(config, request)
                # 预算在建任何模型之前定下：写错的预算要在花钱之前就失败。
                budget = self._budget_policy(
                    config,
                    turns=request.max_turns,
                    tokens=request.max_tokens,
                    cost=request.max_cost,
                    wall_clock_s=request.max_wall_clock,
                    on_exhausted=request.on_exhausted,
                )
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
                per_role = role_policies(judge, config.danger_roles)
                factories = catalog.freeze(
                    tools,
                    models,
                    facts,
                    danger_judge=judge,
                    danger_policy=policy,
                    role_policies=per_role,
                )
                if request.no_write:
                    # 先按正常 run 冻结了一遍：过滤会把拼错的工具名一起滤掉，只读 run 不该放过
                    # 正常 run 会拒的配置。按角色的处置随之保留。
                    catalog = _read_only(catalog)
                    factories = catalog.freeze(
                        tools,
                        models,
                        facts,
                        danger_judge=judge,
                        danger_policy=policy,
                        role_policies=per_role,
                    )
                runtime = Runtime(
                    agents={} if request.topology is not None else factories,
                    orchestration=_ORCHESTRATION,
                    modules=modules,
                    flags=resolved,
                    confirmation_handler=MailboxConfirmationHandler(timeout=_CONFIRMATION_TIMEOUT),
                    tracer=tracer,
                    ids=None if tracer is None else tracer.ids,
                    untrusted_output_modules=untrusted,
                    workspace=workspace,
                    sessions=(
                        FileSessionStore(session_root(self._workspace_root))
                        if tracer is not None
                        else None
                    ),
                    redactor=(
                        host_redactor(self._environ, config.redaction_rules)
                        if tracer is not None
                        else None
                    ),
                    artifacts=self._artifact_index(modules),
                    budget=budget,
                )
                if tracer is not None and tracer.level is not TraceLevel.STRUCTURE:
                    records = self._module_records(modules, config)

                    def profile(roles: Mapping[str, DangerPolicy]) -> dict[str, object]:
                        return host_profile(
                            flags=resolved,
                            judge=judge,
                            policy=policy,
                            output_limit=_OUTPUT_LIMIT,
                            facts=facts,
                            max_turns=None,
                            root_budget=_root_budget_record(budget),
                            no_write=request.no_write,
                            modules=modules,
                            orchestration=_ORCHESTRATION,
                            confirmation_timeout=_CONFIRMATION_TIMEOUT,
                            roles=roles,
                            module_records=records,
                            untrusted_output_modules=untrusted,
                        )

                    self._describe_run(
                        tracer,
                        request,
                        config,
                        catalog,
                        factories,
                        profile,
                        policy,
                        prompt,
                        assembled,
                        modules,
                    )
                if logs is not None:
                    # 服务是装配时 setattr 上去的，Runtime 的静态类型里没有这个属性。
                    logs.append(cast(DevLog, getattr(runtime, 'dev_log')))  # noqa: B009
                subscription = runtime.events.subscribe()
                pump = asyncio.create_task(
                    self._event_pump(runtime, subscription),
                    name='cli-human-events',
                )
                failure: BaseException | None = None
                try:
                    if assembled is not None:
                        topology, root_slot = assembled
                        await runtime.assemble(
                            topology,
                            catalog,
                            tools=tools,
                            models=models,
                            host=facts,
                            danger_judge=judge,
                            danger_policy=policy,
                            role_policies=per_role,
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
                            # 只读 / 交接 run 不落盘成会话：恢复会用不受限 catalog 重建，把人收走的
                            # 写工具又发回去（评审 H5）。不是会话就不可恢复，也就无从升权。
                            session=not request.no_write and not request.handoff,
                        )
                    context = await self._wait_with_pump(runtime, agent_id, pump)
                    if stops is not None:
                        stops.append(context.stop_reason)
                    return await self._answer(
                        runtime,
                        context,
                        runtime_errors,
                        agent_id=agent_id,
                        start=0,
                        fail_without_text=True,
                        reports=reports,
                    )
                except BaseException as exc:
                    failure = exc
                    if tracer is not None and isinstance(exc, asyncio.CancelledError):
                        # 拆 Runtime 之前记下此刻在等外部世界的调用：拆的时候它们都会被取消，
                        # 之后就分不出哪些是被这次打断切断的。
                        tracer.interrupted()
                    raise
                finally:
                    try:
                        await runtime.shutdown()
                    except Exception as error:
                        # Module 的 on_shutdown 坏了不改 run 的结论：答案已经算出来，丢掉它不比
                        # 报出来更有用。
                        self._warn(
                            host_redactor(self._environ, config.redaction_rules).redact(
                                _shutdown_failure(error)
                            )
                        )
                    # Runtime 关了，泵即使收到答复也送不出去；它可能正等着人敲回车（被 Ctrl-C 取消、
                    # 问题已经超时而 Agent 先结束），不取消就一直等下去。
                    if not pump.done():
                        pump.cancel()
                    (outcome,) = await asyncio.gather(pump, return_exceptions=True)
                    subscription.close()
                    # run 自己的错误（含 Ctrl-C 的取消）优先：泵的错误不能把它换掉。
                    if isinstance(outcome, Exception) and outcome is not failure:
                        if failure is None:
                            raise outcome
                        self._warn(
                            f'另外，读人的答复时也出了错：{type(outcome).__name__}: {outcome}。'
                            '它不改变这次 run 的结论，先处理 run 的那个错误。'
                        )
        finally:
            try:
                await workspace.close()
            finally:
                environment.remove()

    async def _run_handoff(self, request: RunRequest, flags: Mapping[str, str]) -> CliOutcome:
        """D13：锁住工作区 → 运行前快照 → run → 复核 → 写交接文件 → git 收尾 → 重写并打印。
        `verify`、`protected`、`pass_env` 都取开始时加载的配置：Agent 中途改 `.syndica.yaml`
        不影响。"""
        config = self._trusted_config(request.config)
        redactor = host_redactor(self._environ, config.redaction_rules)
        try:
            lock = lock_workspace(self._workspace_root)
        except HandoffLocked as exc:
            raise _config_error(
                '同一工作区同时只跑一个 `run --handoff`',
                f'{self._workspace_root} 正被另一个 run 锁着',
                '等那个 run 结束（它退出时会把交接文件路径打到 stderr），或者到别的工作区里跑',
            ) from exc
        ctrl_c = _CtrlC(self._warn)
        self._ctrl_c = ctrl_c
        try:
            with ctrl_c.installed():
                return await self._handoff(request, flags, config, redactor, ctrl_c)
        except Exception as exc:
            raise _handoff_failure(exc, redactor) from exc
        finally:
            os.close(lock)

    async def _handoff(
        self,
        request: RunRequest,
        flags: Mapping[str, str],
        config: CliConfig,
        redactor: Redactor,
        ctrl_c: _CtrlC,
    ) -> CliOutcome:
        root = self._workspace_root
        git = HostGit(root, self._environ)
        in_repository = _in_repository(git)
        start: Integrity | None = None
        ignored_before: frozenset[str] | None = None
        if in_repository and not request.no_write:
            try:
                start = integrity(git)
                ignored_before = ignored_paths(git)
            except (GitUnavailable, OSError):
                pass
        before, before_error = _changes(git)
        logs: list[DevLog] = []
        stops: list[StopReason | None] = []
        reports: list[str] = []
        traces: list[tuple[str, bool]] = []
        answer: str | None = None
        failure: str | None = None
        results: tuple[VerifyResult, ...] = ()
        verify_error: str | None = None
        changed_by_verify: tuple[str, ...] = ()
        original = self._io
        writers = (
            RedactingWriter(original.stdout, redactor),
            RedactingWriter(original.stderr, redactor),
        )
        self._io = CliIO(
            stdin=original.stdin,
            stdout=cast(TextIO, writers[0]),
            stderr=cast(TextIO, writers[1]),
        )
        self._prompts_to_stderr = True
        # Ctrl-C 落在哪一段：交接文件照样写，原因写这一段的说明，复核标 skipped，退出 130。
        interrupted: str | None = None
        try:
            try:
                answer = await _interruptible(
                    ctrl_c,
                    self._run_request(
                        request,
                        flags,
                        config=config,
                        logs=logs,
                        stops=stops,
                        reports=reports,
                        traces=traces,
                    ),
                )
            except asyncio.CancelledError:
                if not ctrl_c.pressed:
                    raise
                interrupted = _INTERRUPTED_RUN
            except Exception as exc:
                failure = _failure_text(exc)
            # 取消在 run 里面被转成了别的错误：同样是被中断。run 交回了回答，按键就落在它做完之后。
            if interrupted is None and ctrl_c.pressed:
                interrupted = _INTERRUPTED_AFTER_RUN if answer is not None else _INTERRUPTED_RUN
            # 「run 正常结束」（D14）指根 Agent 以 completed 结束：轮数用完、被中止时它留下的文字
            # 可能只做了一半。
            if failure is None and stops and stops[0] is not StopReason.COMPLETED:
                reason = stops[0].value if stops[0] is not None else 'unknown'
                failure = f'根 Agent 以 {reason} 结束，不是正常完成：它的回答可能只做了一半。'
            snapshot_a, changes_error = _changes(git)
            verified = False
            if interrupted is not None or failure is not None or request.no_write:
                host_verify = 'skipped'
            elif not config.workspace.verify:
                host_verify = 'not_configured'
            else:
                verified = True
                host_verify = 'failed'
                try:
                    results = await _interruptible(ctrl_c, self._verify(config))
                except asyncio.CancelledError:
                    if not ctrl_c.pressed:
                        raise
                    interrupted = _INTERRUPTED_VERIFY
                except Exception as exc:
                    verify_error = f'复核命令没能跑完：{type(exc).__name__}: {exc}'
                if interrupted is None and ctrl_c.pressed:
                    interrupted = _INTERRUPTED_VERIFY
            if interrupted is not None:
                failure = interrupted
                results = ()
                host_verify = 'skipped'
            elif verified:
                snapshot_b, error_b = _changes(git)
                # 在 git 仓库里而快照 A 或 B 没取到（复核命令弄坏了仓库、留下读不了的文件），就不
                # 知道复核改了什么：按失败算。不在 git 仓库里是本来就比不了，交接文件写明了原因。
                if (
                    verify_error is None
                    and in_repository
                    and (snapshot_a is None or snapshot_b is None)
                ):
                    error = changes_error if snapshot_a is None else error_b
                    verify_error = f'列不出复核前后的改动，确认不了复核命令有没有改文件：{error}'
                changed_by_verify = _differences(snapshot_a, snapshot_b)
                passed = (
                    verify_error is None
                    and all(result.passed for result in results)
                    and not changed_by_verify
                )
                host_verify = 'passed' if passed else 'failed'
        finally:
            for writer in writers:
                writer.flush()
            self._io = original
            self._prompts_to_stderr = False
        log = logs[0].snapshot() if logs else DevLogSnapshot((), (), ())
        verdict = reviewer_verdict(log, snapshot_a)
        status = status_of(
            run_failed=failure is not None,
            read_only=request.no_write,
            host_verify=host_verify,
            verdict=verdict,
        )
        protected = protected_matcher(config.workspace.protected)
        run_changes = _run_changes(before, snapshot_a)
        protected_changed = sorted(change.path for change in run_changes if protected(change.path))
        facts = HandoffFacts(
            prompt=request.prompt,
            answer=answer,
            failure=failure,
            changes=snapshot_a,
            changes_error=changes_error,
            before_error=before_error if in_repository and before is None else None,
            touched_again=_touched_again(before, snapshot_a),
            written=_written(log, root),
            checks=_checks(log),
            verify=results,
            verify_error=verify_error,
            changed_by_verify=changed_by_verify,
            run_errors=tuple(reports),
            next_steps=_next_steps(
                status,
                host_verify,
                verdict,
                results,
                changed_by_verify,
                verify_error,
                changed=bool(run_changes),
            ),
            trace=traces[0][0] if traces else None,
            trace_complete=traces[0][1] if traces else True,
        )

        def document(git_state: str, exit_code: int | None, section: str) -> str:
            matter = front_matter(
                status=status,
                host_verify=host_verify,
                verdict=verdict,
                git=git_state,
                protected_changed=protected_changed,
                exit_code=exit_code,
            )
            return render(matter, facts, git_section=section)

        name = f'{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}.md'
        try:
            path = write_handoff(root, name, document('pending', None, _PENDING), redactor)
        except (HandoffWriteError, OSError) as exc:
            # 结论不能跟着文件一起丢：照样打印到 stdout，退出 1 表示留档没做成。没有留档就不提交。
            self._warn(redactor.redact(f'交接文件没写成，内容照样打印在 stdout 上：\n{exc}'))
            return CliOutcome(redactor.redact(document('not_offered', 1, _NOT_WRITTEN)), 1)
        self._warn(redactor.redact(f'交接文件：{path}'))
        # 写交接文件期间按的 Ctrl-C 只记下了：现在才决定不做 git 收尾。
        if interrupted is None and ctrl_c.pressed:
            interrupted = _INTERRUPTED_NO_GIT
        if request.no_write:
            result = Finish('not_offered', '只读 run 不改动工作区，不做 git 收尾。')
        elif interrupted is not None:
            result = Finish('not_offered', _INTERRUPTED_NO_GIT)
        else:
            stderr = RedactingWriter(self._io.stderr, redactor)
            agent = agent_environment(self._environ, config.workspace.pass_env, workspace_root=root)
            try:
                with ctrl_c.raising() as started:
                    if started:
                        result = finish(
                            git,
                            mode=request.git_mode,
                            start=start,
                            ignored_before=ignored_before,
                            before_changes=before,
                            status=status,
                            host_verify=host_verify,
                            verdict=verdict,
                            run_changes=run_changes,
                            touched_again=facts.touched_again,
                            protected=protected_changed,
                            written=lambda changed: path_key(changed) in facts.written,
                            title=commit_title(answer),
                            prompt=request.prompt,
                            verify=[(item.command, item.summary) for item in results],
                            stdin=self._answers(),
                            stderr=cast(TextIO, stderr),
                            redactor=redactor,
                            hook_env=hook_environment(self._environ, agent.values),
                        )
                    else:
                        interrupted = _INTERRUPTED_NO_GIT
                        result = Finish('not_offered', _INTERRUPTED_NO_GIT)
            except KeyboardInterrupt as exc:
                # 推送到没到远端宿主不知道：磁盘上那份按 M2 D13 停在 `git: pending`，不改写它；
                # 宿主看到的仓库状态写进打印的那份与 stderr。
                seen = exc.detail if isinstance(exc, GitInterrupted) else None
                self._warn(
                    redactor.redact(
                        f'git 收尾被中断（Ctrl-C）：{path} 停在 git: pending'
                        + (f'。{seen}' if seen else '')
                    )
                )
                section = _INTERRUPTED_GIT + (f'\n\n宿主看到的：{seen}' if seen else '')
                return CliOutcome(
                    redactor.redact(document('pending', EXIT_INTERRUPTED, section)),
                    EXIT_INTERRUPTED,
                )
            finally:
                agent.remove()
                stderr.flush()
        # 结论到这里定了：之后的按键不再改变它，打印的交接文件与退出码一致。
        ctrl_c.settle()
        exit_code = exit_code_of(status, result.state)
        if exit_code == 0 and result.refused:
            exit_code = 3
        if interrupted is not None:
            exit_code = EXIT_INTERRUPTED
        final = document(result.state, exit_code, result.section)
        try:
            rewrite_handoff(root, path, final, redactor)
        except (HandoffWriteError, OSError) as exc:
            # git 收尾已经做了（可能已提交、已推送）：结论照样打印，不能让人以为什么都没发生而重跑。
            self._warn(
                redactor.redact(
                    f'交接文件没能更新成最终版本（{path} 里停在 git: pending），最终内容照样打印在 '
                    f'stdout 上：\n{exc}'
                )
            )
            exit_code = EXIT_INTERRUPTED if interrupted is not None else 1
            final = document(
                result.state,
                exit_code,
                f'{result.section}\n\n交接文件没能更新成这一版：磁盘上的那份停在 `git: pending`，'
                '以这里打印的为准；不要为此重跑。',
            )
        return CliOutcome(redactor.redact(final), exit_code)

    async def _verify(self, config: CliConfig) -> tuple[VerifyResult, ...]:
        """在新的工作区（Agent 的环境）里逐条跑 verify：复核验证的是 Agent 能跑出的东西。"""
        workspace, environment = self._open_workspace(config)
        results: list[VerifyResult] = []
        try:
            for command in config.workspace.verify:
                result = await workspace.portal().run(command, timeout=_VERIFY_TIMEOUT, cwd='.')
                check = parse_check(
                    command=command,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                )
                results.append(VerifyResult(command, check.status == 'passed', check.summary))
        finally:
            try:
                await workspace.close()
            finally:
                environment.remove()
        return tuple(results)
