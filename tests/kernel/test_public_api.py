"""`syndica.kernel.__all__` 是公开 API：列进去就是永久兼容承诺，导不进来就是发布即坏。"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
from pathlib import Path

import pytest

import syndica.kernel as kernel
import syndica.kernel.contracts as contracts
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.runtime import Runtime
from syndica.kernel.stores import InMemoryTraceStore
from syndica.kernel.trace_recorder import TraceRecorder

# 逐字写死，**不从 `kernel.__all__` 推导**：删一个导出、加一个导出都必须先在这里变红。用被测对象
# 自己的导出面算出来的期望值（「每个名字都解析得到」「列表有序」）两条都由构造恒成立，测的是恒等式
# 而不是这条承诺。这份清单同时钉住数量、拼写、无重复与落盘顺序。
_KERNEL_EXPORTS: tuple[str, ...] = (
    'AgentCatalog',
    'AgentFactory',
    'AgentInfo',
    'AgentManifest',
    'AgentOptions',
    'AgentRegistry',
    'AgentSlot',
    'AgentStatus',
    'AllowAll',
    'ArtifactRef',
    'AskUser',
    'AskUserQuestion',
    'AttributeValue',
    'BaseAgent',
    'BlockHighRisk',
    'CachePrefixProvider',
    'CausalityEdge',
    'ConfigError',
    'Confirmation',
    'ConfirmationHandler',
    'ConfirmationOutcome',
    'ConfirmationRequest',
    'Context',
    'ContextBlock',
    'ContextOverflowError',
    'ContractViolation',
    'DEFAULT_RULES',
    'DangerJudge',
    'DangerPolicy',
    'DangerRequest',
    'DangerRule',
    'DangerVerdict',
    'DeliveryResult',
    'Disposition',
    'EdgeKind',
    'Event',
    'EventBus',
    'ExecResult',
    'ExitMode',
    'FileTraceStore',
    'Finding',
    'FlagDeclaration',
    'HookCallback',
    'HookResult',
    'HooksCenter',
    'HostFacts',
    'HumanInteractionModule',
    'IdSource',
    'InMemoryAgentRegistry',
    'InMemoryMessageBroker',
    'InMemoryTraceStore',
    'Junction',
    'JunctionKind',
    'JunctionRef',
    'LLM',
    'LLMConfig',
    'LLMResponseError',
    'LocalWorkspace',
    'MailboxConfirmationHandler',
    'ManifestSource',
    'Message',
    'MessageBroker',
    'MissingQueueCallback',
    'NO_RULE_MATCHED',
    'NullTracer',
    'Orchestrator',
    'OverrideRecord',
    'PayloadRef',
    'PayloadText',
    'Plugin',
    'Portal',
    'PrefixDriftModule',
    'PrefixDriftRecord',
    'PrefixDriftService',
    'RedactionRule',
    'Reply',
    'RetryModule',
    'RiskLevel',
    'Role',
    'RuleRedactor',
    'Runtime',
    'RuntimeModule',
    'SafetyModule',
    'ScenarioDocument',
    'ScriptedLLM',
    'ScriptedTurn',
    'Span',
    'SpanEvent',
    'SpanKind',
    'SpanStatus',
    'StableKey',
    'StopReason',
    'Subscription',
    'SyndicaError',
    'Tool',
    'ToolCall',
    'ToolFatalError',
    'ToolResult',
    'Topology',
    'TraceHeader',
    'TraceLevel',
    'TraceRecorder',
    'TraceRedaction',
    'TraceSource',
    'TraceStore',
    'Tracer',
    'Trigger',
    'TrustLevel',
    'UserInput',
    'WaitResult',
    'WaitState',
    'Waiter',
    'Workspace',
    'local_tools',
    'orchestration_tools',
    'parse_scenario_document',
    'provenance_of',
    'register_builtins',
    'strictest_trust',
)
# contracts 随 wheel 发布，是第三方写 Store 实现的入口（spec §13.3），同属兼容承诺。
_CONTRACT_EXPORTS: tuple[str, ...] = (
    'AgentRegistryContract',
    'ContractSuite',
    'MessageBrokerContract',
    'TraceStoreContract',
    'UPDATE_PROTECTED_FIELDS',
    'UPDATE_PROTECTED_FIELD_OWNERS',
    'UPDATE_WRITABLE_FIELDS',
)
# `Runtime` 的公开面：**spec §4.2 把它刻意收紧到 11 个方法**，每加一个都是一条永久兼容承诺。
# 逐字写死，理由同 `_KERNEL_EXPORTS`——这份清单是唯一会因为「顺手加一个公开方法」而变红的东西，
# 而 `kernel.__all__` 对 `Runtime` 上新增的名字完全无感（它只导出 `Runtime` 这一个名字）。
#
# 11 个之外多出来的四项各有来路：`shutdown`（Runtime 自己的生命周期，spec 全文以它收尾）、
# `plugins`（装配的第二层，spec §5.3）、`agent_factory` / `agent_names`（角色表读面，spec §4.7
# 拓扑那一侧），以及四个构造时就交给宿主的协作方对象。在轮等待与分叉的那几个接缝刻意**不在**这里
# ——加一个公开方法不是破坏性变更，去掉一个是。
_RUNTIME_SURFACE: tuple[str, ...] = (
    'abort',
    'agent_factory',
    'agent_names',
    'assemble',
    'broker',
    'despawn',
    'ensure_active',
    'events',
    'flag_declarations',
    'flags',
    'find',
    'hooks',
    'kill',
    'modules',
    'occupy',
    'pause',
    'plugins',
    'registry',
    'resolve',
    'resume',
    'send',
    'send_to_slot',
    'shutdown',
    'spawn',
    'wait',
    'wait_for',
)
# `BaseAgent` 的公开面。`kernel.__all__` 只导出类名这一个字符串，对类上新增的方法完全无感——
# 一次「顺手加个公开方法」因此在这里之外没有任何东西会变红。
_BASE_AGENT_SURFACE: tuple[str, ...] = (
    'agent_id',
    'context',
    'get_system_prompt',
    'install_own_tools',
    'install_runtime_tools',
    'options',
    'register_hook',
    'run',
)
# `TraceRecorder` 的公开面：宿主的生命周期（begin / finish / dropped / trace_id / level）加
# `Tracer` 协议的七个方法。
_TRACE_RECORDER_SURFACE: tuple[str, ...] = (
    'add_edge',
    'add_event',
    'begin',
    'describe_run',
    'dropped',
    'end_span',
    'finish',
    'ids',
    'interrupted',
    'level',
    'llm_exchange',
    'start_span',
    'tool_result',
    'trace_id',
    'wait_answer',
)
# `Runtime.__init__` / `BaseAgent.__init__` 的参数名、种类、有没有默认值。`dir()` 看不见构造
# 签名：加一个关键字参数、把必填改成可选、把 keyword-only 改成位置参数，公开面守卫全绿。
# 第三列是「有没有默认值」而不是默认值本身——默认值改了是行为变更，这条守卫管的是形状。
_RUNTIME_INIT: tuple[tuple[str, str, bool], ...] = (
    ('self', 'POSITIONAL_OR_KEYWORD', False),
    ('registry', 'KEYWORD_ONLY', True),
    ('broker', 'KEYWORD_ONLY', True),
    ('plugins', 'KEYWORD_ONLY', True),
    ('agents', 'KEYWORD_ONLY', True),
    ('orchestration', 'KEYWORD_ONLY', True),
    ('modules', 'KEYWORD_ONLY', True),
    ('flags', 'KEYWORD_ONLY', True),
    ('confirmation_handler', 'KEYWORD_ONLY', True),
    ('ids', 'KEYWORD_ONLY', True),
    ('tracer', 'KEYWORD_ONLY', True),
)
_RUNTIME_SPAWN: tuple[tuple[str, str, bool], ...] = (
    ('self', 'POSITIONAL_OR_KEYWORD', False),
    ('factory', 'POSITIONAL_OR_KEYWORD', False),
    ('query', 'KEYWORD_ONLY', True),
    ('query_trust', 'KEYWORD_ONLY', True),
    ('exit_mode', 'KEYWORD_ONLY', True),
    ('parent_id', 'KEYWORD_ONLY', True),
    ('fork_from', 'KEYWORD_ONLY', True),
    ('metadata', 'KEYWORD_ONLY', True),
    ('flags', 'KEYWORD_ONLY', True),
)
_BASE_AGENT_INIT: tuple[tuple[str, str, bool], ...] = (
    ('self', 'POSITIONAL_OR_KEYWORD', False),
    ('options', 'POSITIONAL_OR_KEYWORD', False),
    ('hooks', 'KEYWORD_ONLY', False),
    ('events', 'KEYWORD_ONLY', False),
)
# `AgentOptions` 的公开面，理由同上；它是宿主唯一亲手填的那个数据类。
_AGENT_OPTIONS_SURFACE: tuple[str, ...] = (
    'danger_judge',
    'danger_policy',
    'llm',
    'max_tool_concurrency',
    'max_turns',
    'name',
    'system_prompt',
    'tools',
)
# **字段顺序**另钉一条：`dir()` 是排序过的，插在中间与追加在末尾在它眼里是同一个答案，而
# `AgentOptions` 的字段顺序计入公开 API（位置实参照着它写）。
_AGENT_OPTIONS_FIELDS: tuple[str, ...] = (
    'name',
    'llm',
    'tools',
    'system_prompt',
    'max_turns',
    'danger_judge',
    'danger_policy',
    'max_tool_concurrency',
)


_PACKAGE = Path(__file__).resolve().parents[2] / 'src' / 'syndica' / 'kernel'
# 子模块导出、却**刻意不**进 `kernel.__all__` 的名字，逐字写死。判据见 `kernel/__init__.py`：
# 一个名字进公开面，当且仅当宿主拿不到它就用不了某个已经公开的入口。下面每一项都答「用得了」：
#
# - 门的四件套（`TurnGate` / `NullTurnGate` / `BrokerTurnGate` / `TurnGateDecision`）与
#   `ControlBlock`：控制面自己的接缝，`Runtime` 是唯一的生产者、`BaseAgent` 是唯一的消费者。
#   装门走 `_attach_gate`，不在公开面；宿主不传门时默认就是 `NullTurnGate`。
# - 校验函数（`validate_*`、`tool_name_disagreement`）：收下配置的那一层自己调，宿主拿它没有用。
# - `ToolExecutor` / `DEFAULT_MAX_TOOL_CONCURRENCY`：执行器由 `BaseAgent` 构造，上限的默认值经
#   `AgentOptions` 的字段默认值就取得到。
# - 五个编排工具类：`orchestration_tools` 是它们唯一的工厂，交回的是 `dict[str, Tool]`，
#   而 `Tool` 已经在公开面上；宿主要装一个子集按键挑即可，不需要类。
# - `install_plugins`：`Runtime(plugins=...)` 是它唯一的公开入口。
# - `INTERCEPTABLE` / `OBSERVABLE` / `ReadOnlyKwarg`：分类表是内核自己分发时用的，
#   `ReadOnlyKwarg` 是 `dict` 子类、hook 作者按 `dict` 用它。
# - `LocalAgentTable`：spec §4.3 明文说它只在进程内有意义、不进 Protocol。
# - `correlation_of`：`Waiter` 密封分发的内部收口，`wait_for` 自己调。
# - 八个工具类不进承诺面，宿主用 `local_tools` 按键取。
# - `RuleTablePolicy`：M2 开发期宿主用的按表处置；进不进承诺面在 M12 冻结公开 API 时定。
# - 三个规则工厂（`git_state_change_rule` 等）：M2 开发期宿主组合的可选规则，同上待 M12 定。
# - `RedactedText` / `encode_redacted`：还没有公开入口收它们（trace 的写入面是第一个，到那时再判）。
# - `Redactor`：还没有公开入口收一个脱敏器，宿主直接用 `RuleRedactor`。第一个收它的入口（trace 的
#   写入面）还要规则集的摘要，协议的形状到那时连同这个成员一起定。
# - `RULE_IDS` / `RULES_VERSION`：宿主不需要它们就能构造与使用脱敏器；两者都算进了
#   `RuleRedactor.digest`，判断两套规则是不是同一套比摘要即可。
# - `looks_secret`：宿主从自己的环境里挑密钥值时用；内核的公开入口只收挑好的值。
# - `write_all` / `write_redacted`：包内落盘只经它们（写入点守卫认得的写法）；宿主不需要它们就能
#   用任何一个公开入口。
# - `RandomIds`：`Runtime(ids=None)` 的默认值，宿主不传就是它。「当前工具调用」与它的那几个进出
#   函数（`current_tool_call`、`ToolCallKey`、`entering_tool_call` 等）是内核插桩与回放的接缝，
#   trace 的公开面定下来时再判。
# - trace 的模型、`TraceStore` 协议、内存实现与它的契约套件：还没有公开入口收一个 TraceStore。第一个
#   是 `TraceRecorder`，trace 的公开面到那时一起定，契约套件随协议一起进承诺面。
# - 场景文档各部分的类型（`ScenarioRole`、`ToolOutcome`、`HostProfile` 等）与 schema 的常量：
#   回放（T13）与捕获（T14）还会动它们的形状。入口（`parse_scenario_document`、`ScenarioDocument`、
#   `PayloadText`）先进承诺面，各部分到捕获写出的文档定稿时再判。
# - `RequestFingerprints` 与 `arguments_digest`：回放（runner）用它们对自己的请求与执行的参数取与
#   记录时同样的指纹；宿主不需要它们就能记录与回放。
# - `encode_prepared`：recorder 写回放文档用的编码（各项已在自己的上下文里脱敏）；宿主写自己的
#   持久化内容用 `encode_redacted`。
# - 回放文档的账本（`ReplayCapture`、`RecordingIds`、`Unreplayable`）：宿主经 `TraceRecorder.ids` 与
#   `describe_run` 用它们，不直接碰；`REPLAYED_TOOLS` 是捕获与回放共用的名单，宿主不读它。
_WITHHELD: tuple[str, ...] = (
    'ATTRIBUTE_DOMAINS',
    'ATTRIBUTE_KINDS',
    'Bash',
    'BashOutput',
    'BrokerTurnGate',
    'ControlBlock',
    'DEFAULT_MAX_TOOL_CONCURRENCY',
    'DangerRuleSpec',
    'Delegate',
    'Edit',
    'FROZEN_IMPLS',
    'Fork',
    'FrozenRole',
    'GateRecord',
    'Glob',
    'Grep',
    'HostAction',
    'HostProfile',
    'INTERCEPTABLE',
    'KillAgent',
    'KillBash',
    'LocalAgentTable',
    'NullTurnGate',
    'OBSERVABLE',
    'QueryAgent',
    'REPLAYED_TOOLS',
    'RULES_VERSION',
    'RULE_IDS',
    'RULE_PARAMETERS',
    'RandomIds',
    'Read',
    'ReadOnlyKwarg',
    'RecordingIds',
    'RedactedText',
    'Redactor',
    'ReplayCapture',
    'RequestFingerprints',
    'RuleTablePolicy',
    'SCENARIO_VERSIONS',
    'ScenarioCall',
    'ScenarioEntry',
    'ScenarioError',
    'ScenarioExpectation',
    'ScenarioInstance',
    'ScenarioReply',
    'ScenarioResponse',
    'ScenarioRole',
    'ScenarioSource',
    'SendMessage',
    'SkippedCall',
    'TRACE_SCHEMA',
    'Text',
    'ToolCallKey',
    'ToolExecutor',
    'ToolOutcome',
    'TurnGate',
    'TurnGateDecision',
    'Unreplayable',
    'Write',
    'arguments_digest',
    'assigned_agent_id',
    'assigning_agent_id',
    'back_in_spans',
    'becoming_owner',
    'correlation_of',
    'current_llm_call',
    'current_span_id',
    'current_tool_call',
    'disposition_of',
    'encode_digests',
    'encode_prepared',
    'encode_redacted',
    'entering_owner',
    'entering_tool_call',
    'git_state_change_rule',
    'install_plugins',
    'leaving_owner',
    'leaving_tool_call',
    'llm_attempt',
    'looks_secret',
    'next_key',
    'outside_any_span',
    'parse_mapping',
    'path_outside_workspace_rule',
    'peek_key',
    'redact_argv',
    'register_tool',
    'releasing_agent_id',
    'resolve_tool_names',
    'span_id_of',
    'tool_name_disagreement',
    'validate_agent_factories',
    'validate_danger_options',
    'validate_max_concurrency',
    'validate_tool_names',
    'workspace_root_removal_rule',
    'write_all',
    'write_redacted',
)


def _declared_exports() -> tuple[dict[str, set[str]], list[str]]:
    """({名字: 声明它的子模块}, 本次真的扫过的文件)。

    期望值来自**文件树**，不是包自己的 re-export 面：拿 `dir(kernel)` 或任何一个 `__init__`
    去推，漏掉一个子模块的名字会让期望值跟着缩水。

    `__all__` 必须是一份字面量字符串列表。`__all__ += [...]`、`__all__.extend(...)`、
    `[*__all__, 'x']` 都会让扫描少看见名字，而主断言全绿——那些名字从此既不进承诺清单、
    也不进扣留清单。
    """
    declared: dict[str, set[str]] = {}
    scanned: list[str] = []
    for path in sorted(_PACKAGE.rglob('*.py')):
        relative = path.relative_to(_PACKAGE).as_posix()
        scanned.append(relative)
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if (
                isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == '__all__'
            ):
                pytest.fail(
                    f'{relative}: `__all__` is mutated after assignment; '
                    'the export scan cannot see the extra names'
                )
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == '__all__'
                and node.value.func.attr in {'append', 'extend', 'insert'}
            ):
                pytest.fail(
                    f'{relative}: `__all__.{node.value.func.attr}(...)` mutates the export '
                    'list after assignment; the export scan cannot see the extra names'
                )
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == '__all__' for target in node.targets
            ):
                value = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == '__all__'
            ):
                if node.value is None:
                    pytest.fail(f'{relative}: `__all__` is annotated but not assigned')
                value = node.value
            if value is None:
                continue
            if not isinstance(value, ast.List):
                pytest.fail(
                    f'{relative}: `__all__` must be a list literal; got {type(value).__name__}'
                )
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    declared.setdefault(element.value, set()).add(relative)
                    continue
                pytest.fail(
                    f'{relative}: `__all__` entries must be string literals; '
                    f'got {type(element).__name__}'
                )
    return declared, scanned


def _init_shape(cls: type) -> tuple[tuple[str, str, bool], ...]:
    return tuple(
        (parameter.name, parameter.kind.name, parameter.default is not inspect.Parameter.empty)
        for parameter in inspect.signature(cls.__init__).parameters.values()
    )


def _package_python_files() -> set[str]:
    """包里全部 `.py`，用 `os.walk` 独立走一遍：范围被收窄时两份清单只有同时改才对得上。"""
    found: set[str] = set()
    for root, _, names in os.walk(_PACKAGE):
        for name in names:
            if name.endswith('.py'):
                found.add((Path(root) / name).relative_to(_PACKAGE).as_posix())
    return found


def _agent() -> BaseAgent:
    return BaseAgent(
        AgentOptions(name='surface', llm=ScriptedLLM([])),
        hooks=HooksCenter(),
        events=EventBus(),
    )


def test_every_exported_name_resolves():
    assert [name for name in kernel.__all__ if not hasattr(kernel, name)] == []


def test_kernel_exports_workspace_types_and_local_tools():
    from syndica.kernel.local_workspace import LocalWorkspace
    from syndica.kernel.tools.local import local_tools
    from syndica.kernel.workspace import ExecResult, Portal, Workspace

    assert kernel.ExecResult is ExecResult
    assert kernel.LocalWorkspace is LocalWorkspace
    assert kernel.Portal is Portal
    assert kernel.Workspace is Workspace
    assert kernel.local_tools is local_tools


def test_kernel_exports_the_human_tool_definitions_not_aliases():
    from syndica.kernel.tools.human import AskUser, AskUserQuestion, HumanInteractionModule

    assert kernel.AskUser is AskUser
    assert kernel.AskUserQuestion is AskUserQuestion
    assert kernel.HumanInteractionModule is HumanInteractionModule


def test_kernel_exports_the_confirmation_definitions_not_aliases():
    from syndica.kernel.danger import (
        ConfirmationHandler,
        ConfirmationOutcome,
        ConfirmationRequest,
        MailboxConfirmationHandler,
    )

    assert kernel.ConfirmationHandler is ConfirmationHandler
    assert kernel.ConfirmationOutcome is ConfirmationOutcome
    assert kernel.ConfirmationRequest is ConfirmationRequest
    assert kernel.MailboxConfirmationHandler is MailboxConfirmationHandler


def test_export_list_is_sorted():
    assert list(kernel.__all__) == sorted(kernel.__all__)


def test_the_public_surface_is_exactly_this():
    assert list(kernel.__all__) == list(_KERNEL_EXPORTS)


def test_the_contract_suite_surface_is_exactly_this():
    assert list(contracts.__all__) == list(_CONTRACT_EXPORTS)
    assert [name for name in contracts.__all__ if not hasattr(contracts, name)] == []


@pytest.mark.parametrize('orchestration', [False, True], ids=['default', 'orchestration'])
def test_the_runtime_surface_is_exactly_this(orchestration: bool):
    """按实例枚举而不是按类：`registry` / `broker` / `events` / `hooks` 是 `__init__` 里赋的
    实例属性，只看 `dir(Runtime)` 会漏掉它们，而宿主读写的正是这四个。

    `orchestration=True` 必须交出同一张公开面：装配五个工具是 spawn 时的动作，不得在 Runtime
    实例上多出一个名字。
    """
    runtime = Runtime(orchestration=orchestration)
    assert sorted(name for name in dir(runtime) if not name.startswith('_')) == sorted(
        _RUNTIME_SURFACE
    )


async def test_a_module_adds_exactly_its_declared_runtime_services():
    from syndica.kernel.modules import RuntimeModule

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'clock',
                services={'clock_service': lambda *_: object()},
            )
        ]
    )
    try:
        assert sorted(name for name in dir(runtime) if not name.startswith('_')) == sorted(
            (*_RUNTIME_SURFACE, 'clock_service')
        )
    finally:
        await runtime.shutdown()


def test_the_base_agent_surface_is_exactly_this():
    """同样按实例：`agent_id` / `options` 是 `__init__` 里赋的，而宿主读的正是它们。"""
    assert sorted(name for name in dir(_agent()) if not name.startswith('_')) == sorted(
        _BASE_AGENT_SURFACE
    )


def test_the_trace_recorder_surface_is_exactly_this():
    """按实例：`TraceRecorder` 随 `kernel.__all__` 只导出类名，类上新增的方法在别处都不会变红。"""
    recorder = TraceRecorder(InMemoryTraceStore(), RuleRedactor())
    assert sorted(name for name in dir(recorder) if not name.startswith('_')) == sorted(
        _TRACE_RECORDER_SURFACE
    )


def test_the_agent_options_surface_is_exactly_this():
    options = AgentOptions(name='surface', llm=ScriptedLLM([]))
    assert sorted(name for name in dir(options) if not name.startswith('_')) == sorted(
        _AGENT_OPTIONS_SURFACE
    )


def test_the_agent_options_field_order_is_exactly_this():
    """新字段只能追加在末尾：插在中间会让每一个按位置传参的调用方静默改变含义。"""
    assert [field.name for field in dataclasses.fields(AgentOptions)] == list(_AGENT_OPTIONS_FIELDS)


def test_the_runtime_constructor_shape_is_exactly_this():
    assert _init_shape(Runtime) == _RUNTIME_INIT


def test_the_runtime_spawn_shape_is_exactly_this():
    assert (
        tuple(
            (
                parameter.name,
                parameter.kind.name,
                parameter.default is not inspect.Parameter.empty,
            )
            for parameter in inspect.signature(Runtime.spawn).parameters.values()
        )
        == _RUNTIME_SPAWN
    )


def test_the_base_agent_constructor_shape_is_exactly_this():
    assert _init_shape(BaseAgent) == _BASE_AGENT_INIT


def test_the_export_scan_really_covers_the_whole_package():
    """守卫的自检：一个子模块掉出扫描范围时，它导出的名字既不在承诺清单里、也不在扣留清单里，
    而下面那条断言看不见它——只有在这里把范围与一次独立的遍历对上才抓得住。"""
    declared, scanned = _declared_exports()
    assert set(scanned) == _package_python_files()
    # 每个文件都声明了导出面：不声明的那个对下面那道扫描完全隐身，它里面的名字于是既不进承诺
    # 清单也不进扣留清单，而两边都不会出声。
    declaring = {module for modules in declared.values() for module in modules}
    assert sorted(set(scanned) - declaring) == []


def test_every_name_a_submodule_exports_is_either_promised_or_withheld_on_purpose():
    """公开面是一次次裁决的结果，不是一次整理。往任何子模块的 `__all__` 里加一个名字，这里就得
    回答一次「它进不进永久兼容承诺」——不回答即变红。

    承诺面有两处：`kernel.__all__`，以及随 wheel 发布给第三方写 Store 实现的 `contracts.__all__`
    （spec §13.3）。
    """
    declared, _ = _declared_exports()
    promised = set(kernel.__all__) | set(contracts.__all__)
    assert sorted(set(declared) - promised) == sorted(_WITHHELD)


# 计划 3 裁决 9 点名要进承诺面的名字。逐字写死，不从 kernel.__all__ 推导：
# 少导出一个，差集非空；多导出一个由 test_the_public_surface_is_exactly_this 抓住。
_PLAN3_PROMISED: tuple[str, ...] = (
    'AgentCatalog',
    'AgentManifest',
    'AgentSlot',
    'HostFacts',
    'ManifestSource',
    'Orchestrator',
    'OverrideRecord',
    'Topology',
    'register_builtins',
)


def test_promised_names_include_catalog_topology_and_orchestrator():
    missing = [name for name in _PLAN3_PROMISED if name not in kernel.__all__]
    assert missing == []


def test_no_name_is_both_promised_and_withheld():
    """两份清单必须互斥：一个名字同时出现在两边时，上一条会因为差集少一项而变红，而读者看到的
    却是「它在扣留清单里」——把这条矛盾单独判出来，才不用靠另一条断言的算术去发现它。"""
    assert sorted(set(_WITHHELD) & set(kernel.__all__)) == []
