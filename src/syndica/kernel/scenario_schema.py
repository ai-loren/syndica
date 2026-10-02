"""场景文档的 schema（M3 D12）：手写的 L2 场景与捕获的回放文档共用一份。

纯映射加校验：收一个已经解析好的 Mapping（runner 从 `.yaml` 读手写的、从 `.json` 读捕获的），
交回不可变的 `ScenarioDocument`。不读文件、不依赖 PyYAML：内核不能依赖它，PyYAML 读 JSON 也会把
非 ASCII 字符弄坏。每个错误都是三件套 `ConfigError`，位置写在 `expected` / `received` 里
（`agents.Worker.instances[0]...`）。

v1 是手写脚本：只存回复的一侧，工具真跑，人的答复按请求的先后。v2 是它的超集，多出回放要的东西：

- `host`：宿主档案（`flags`、有序的 `danger.rules` 与 `danger.table`、`output_limit`、
  `host_facts`、`max_turns`、`no_write`、`modules`）；
- `agents.<角色>.frozen`：冻结后的角色（系统提示词正文、有序的工具名、轮数、`impl`），
  代替 `manifest`；
- `instances[].key`：创建这个实例的稳定键（内核 `ids` 的原样数组）；`turns[]` 多出 `turn` /
  `attempt`（「当前 LLM 调用」的键）、`extra`、`trust`、`request_digests`、带类型的 `raises`，
  `content` 可以是列表，`tool_calls[].arguments` 可以是解析不了的原串；`tool_results` /
  `skipped` / `gate` 按调用位置（`[周期, 轮, 批内位置]`）；
- `responses` 可以按请求的稳定键给（`kind` 多一个 `timeout`）；`host_actions`（`abort` 按请求的
  键，`interrupt` / `shutdown` 在 run 安静下来的那一处）；`ids`（稳定键 → 记录的 id）；
  `entry.mode`（`slot` / `query` / `send`）与 `entry.trust`；拓扑槽不再受 v1 的限制；
  `source`（捕获自哪个 trace）。

自由文本（入口提示词、冻结的系统提示词、回复正文、工具输出、人的答复、错误消息）可以内联，也可以
写成 `{payload: <sha256>}` 引用 trace 里的 payload 文件（`PayloadText`，由读它的一方解析）；
占位符 `[REDACTED:…]` 是合法内容。
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from syndica.kernel.catalog import HostFacts
from syndica.kernel.errors import ConfigError
from syndica.kernel.topology import AgentSlot, ExitMode, Topology
from syndica.kernel.trace import ATTRIBUTE_DOMAINS

__all__ = [
    'FROZEN_IMPLS',
    'REPLAYED_TOOLS',
    'RULE_PARAMETERS',
    'SCENARIO_VERSIONS',
    'DangerRuleSpec',
    'FrozenRole',
    'GateRecord',
    'HostAction',
    'HostProfile',
    'PayloadText',
    'ScenarioCall',
    'ScenarioDocument',
    'ScenarioEntry',
    'ScenarioError',
    'ScenarioExpectation',
    'ScenarioInstance',
    'ScenarioReply',
    'ScenarioResponse',
    'ScenarioRole',
    'ScenarioSource',
    'SkippedCall',
    'ToolOutcome',
    'Text',
    'parse_scenario_document',
]

SCENARIO_VERSIONS = (1, 2)
# 冻结角色的 `impl` 只认内核内置的类式角色：回放文档可能来自别人的仓库，任意 import 路径等于执行它。
FROZEN_IMPLS = frozenset({'syndica.kernel.agents.orchestrator:Orchestrator'})
# 触达外部世界、回放时按记录给出结局的工具；其余（派活、消息、人机）是内核，回放时真跑。捕获只记
# 它们的结局，回放只替换它们：两边认的是同一份名单。
REPLAYED_TOOLS = frozenset(
    {'Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'BashOutput', 'KillBash', 'QueryAgent'}
)
# 回放重建得出的危险规则与它们的参数：只有工作区根的两条带参数，`root` 是字面值、`real` 是记录时
# 解析出的路径（回放不在自己的机器上重新解析符号链接）。
RULE_PARAMETERS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        'shell.privilege_escalation': frozenset(),
        'shell.remote_script_execution': frozenset(),
        'shell.recursive_force_remove': frozenset(),
        'shell.raw_device_write': frozenset(),
        'shell.git_state_change': frozenset(),
        'shell.path_outside_workspace': frozenset({'root', 'real'}),
        'shell.workspace_root_removal': frozenset({'root', 'real'}),
    }
)
_REQUIRED_RULE_PARAMETERS = frozenset({'root'})

_V1_ROOT = frozenset({'version', 'name', 'agents', 'topology', 'entry', 'responses', 'expect'})
_V2_ROOT = _V1_ROOT | frozenset({'host', 'host_actions', 'ids', 'source'})
_V2_REQUIRED_ROOT = frozenset({'version', 'name', 'agents', 'entry', 'expect'})
_V1_AGENT = frozenset({'manifest', 'instances'})
_V2_AGENT = _V1_AGENT | frozenset({'frozen'})
_V1_INSTANCE = frozenset({'turns'})
_V2_INSTANCE = _V1_INSTANCE | frozenset({'key', 'tool_results', 'skipped', 'gate'})
_V1_TURN = frozenset({'content', 'tool_calls', 'raises'})
_V2_TURN = _V1_TURN | frozenset({'turn', 'attempt', 'extra', 'trust', 'request_digests'})
_V1_CALL = frozenset({'id', 'name', 'arguments'})
_V2_CALL = _V1_CALL | frozenset({'arguments_raw'})
_V1_ERROR = frozenset({'kind', 'message'})
_V2_ERROR = frozenset({'type', 'message', 'status_code', 'retry_after'})
_ERROR_KINDS: Mapping[str, str] = MappingProxyType(
    {
        'runtime_error': 'builtins.RuntimeError',
        'context_overflow': 'syndica.kernel.llm.base.ContextOverflowError',
        'llm_response_error': 'syndica.kernel.errors.LLMResponseError',
    }
)
_FROZEN = frozenset({'system_prompt', 'tools', 'max_turns', 'impl', 'model', 'description'})
_OUTCOME = frozenset({'call', 'output', 'meta', 'trust', 'artifacts', 'raised', 'arguments_digest'})
_SKIPPED = frozenset({'call', 'output', 'meta', 'trust', 'artifacts'})
_GATE = frozenset({'call', 'rule_ids', 'risk', 'provenance', 'decided_by', 'disposition'})
_GATE_OPTIONAL = frozenset({'outcome'})
_ARTIFACT = frozenset({'id', 'path', 'kind', 'mime', 'version', 'producer', 'is_deliverable'})
_TOPOLOGY = frozenset({'slots'})
_SLOT = frozenset(
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
_V1_ENTRY = frozenset({'slot', 'prompt'})
_V2_ENTRY = _V1_ENTRY | frozenset({'mode', 'agent', 'trust'})
_ENTRY_MODES = ('slot', 'query', 'send')
_V1_RESPONSE = frozenset({'kind', 'agent', 'answer', 'tool_call_id'})
_KEYED_RESPONSE = frozenset({'key', 'kind', 'answer', 'trust'})
_V1_RESPONSE_KINDS = ('user_input', 'user_question', 'confirmation')
_V2_RESPONSE_KINDS = (*_V1_RESPONSE_KINDS, 'timeout')
_HOST_ACTION = frozenset({'kind', 'key', 'waiting'})
_HOST_ACTION_KINDS = ('abort', 'interrupt', 'shutdown')
_ID = frozenset({'key', 'id'})
_HOST = frozenset(
    {'flags', 'danger', 'output_limit', 'host_facts', 'max_turns', 'no_write', 'modules'}
)
_DANGER = frozenset({'rules', 'table'})
_RULE = frozenset({'id', 'params'})
_HOST_FACTS = frozenset({'os', 'platform', 'shell'})
_SOURCE = frozenset({'trace_id', 'argv', 'version', 'config_sha256', 'git_commit'})
_EXPECT = frozenset(
    {'final_output', 'tool_order', 'stop_reasons', 'errors', 'statuses', 'events', 'workspace'}
)
_TRUST = ('trusted', 'untrusted')
_DISPOSITIONS = ('allow', 'block', 'confirm')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_TRACE_ID = re.compile(r'[0-9a-f]{32}')
_NODES: ContextVar[list[int] | None] = ContextVar('syndica_scenario_nodes', default=None)
# 一份 JSON 值最多嵌套这么深、整份文档最多这么多个节点：回放文档可能来自别人的仓库，成环或指数展开的
# 输入不能把读它的进程拖住。
_MAX_DEPTH = 64
_MAX_NODES = 1_000_000
_QUALIFIED = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+')


def _no_entries() -> Mapping[Any, Any]:
    return MappingProxyType({})


@dataclass(frozen=True)
class PayloadText:
    """`{payload: <sha256>}`：正文在 trace 的 `payloads/<sha256>.txt` 里，由读文档的一方解析。"""

    digest: str


Text = str | PayloadText


@dataclass(frozen=True)
class ScenarioError:
    """一次带类型的失败：`type` 是 `模块.限定名`（v1 的 `kind` 换成对应的类型），重建由读它的一方按
    自己的白名单做，schema 不 import 任何东西。"""

    type: str
    message: Text
    status_code: int | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class ScenarioCall:
    """模型给的一次工具调用。`arguments` 是交给工具的那段 JSON 文本：写成 Mapping 的按原来的键序
    紧凑编码，v2 里解析不了的原串原样保留。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ScenarioReply:
    """一次模型调用交回的东西：回复，或 `raises`。`turn` / `attempt` 是「当前 LLM 调用」的键，捕获的
    文档每一项都有、手写的都没有（按先后消费）。"""

    content: Text | tuple[object, ...] | None = None
    tool_calls: tuple[ScenarioCall, ...] = ()
    raises: ScenarioError | None = None
    turn: int | None = None
    attempt: int | None = None
    extra: Mapping[str, object] | None = None
    trust: str = 'trusted'
    request_digests: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具调用自己交回的结局（`POST_TOOL_USE` 改写之前）：结果，或它抛出的异常。`call` 是
    （周期、轮、批内位置），Agent 由所在的实例给出。`arguments_digest` 是这次真正执行的参数（hook
    与确认改写之后）逐叶脱敏、规范编码后的 sha256：模型那一侧比的是发出的请求，工具这一侧比的是
    执行的参数。"""

    call: tuple[int, int, int]
    output: Text | tuple[object, ...] | None = None
    meta: Mapping[str, object] = dataclasses.field(default_factory=_no_entries)
    trust: str = 'trusted'
    artifacts: tuple[Mapping[str, object], ...] = ()
    raised: ScenarioError | None = None
    arguments_digest: str | None = None


@dataclass(frozen=True)
class SkippedCall:
    """被 `PRE_TOOL_USE` 跳过的调用与跳过它的那个结果。"""

    call: tuple[int, int, int]
    output: Text | tuple[object, ...]
    meta: Mapping[str, object] = dataclasses.field(default_factory=_no_entries)
    trust: str = 'trusted'
    artifacts: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class GateRecord:
    """一次闸门判定：命中的规则（没有时是空表）、风险、溯源、由谁决定、处置；`outcome` 是这次调用
    的结局（trace 的 `tool.outcome`：确认被拒、超时、被抢占都在这里分得开）。"""

    call: tuple[int, int, int]
    rule_ids: tuple[str, ...]
    risk: str
    provenance: str
    decided_by: str
    disposition: str
    outcome: str | None = None


@dataclass(frozen=True)
class ScenarioInstance:
    """一个角色的一个运行时实例。`key` 是创建它的稳定键（v2，可选）。"""

    replies: tuple[ScenarioReply, ...]
    key: tuple[str | int, ...] | None = None
    tool_results: tuple[ToolOutcome, ...] = ()
    skipped: tuple[SkippedCall, ...] = ()
    gate: tuple[GateRecord, ...] = ()


@dataclass(frozen=True)
class FrozenRole:
    """冻结之后的角色（v2）：系统提示词是插值之后的正文，工具按声明的顺序。"""

    system_prompt: Text
    tools: tuple[str, ...]
    max_turns: int | None = None
    impl: str | None = None
    model: str = 'default'
    description: str | None = None


@dataclass(frozen=True)
class ScenarioRole:
    """`agents.<名字>`：`manifest`（声明式清单，交给目录加载）与 `frozen` 恰好一个。"""

    name: str
    instances: tuple[ScenarioInstance, ...]
    manifest: Mapping[str, object] | None = None
    frozen: FrozenRole | None = None


@dataclass(frozen=True)
class ScenarioEntry:
    """怎么开始：`slot` 经拓扑槽，`query` / `send` 直接建 `agent` 这个角色（建时带上提示词，或建完再
    发）。"""

    prompt: Text
    mode: str = 'slot'
    slot: str | None = None
    agent: str | None = None
    trust: str = 'trusted'


@dataclass(frozen=True)
class ScenarioResponse:
    """一条人的答复。v1 的写法按请求的先后、以 `agent` 选择器（`角色[第几个]`）与确认的
    `tool_call_id` 对上；v2 也可以按请求的稳定键 `key` 给。`timeout` 没有答复。"""

    kind: str
    answer: Text | None = None
    agent: str | None = None
    tool_call_id: str | None = None
    key: tuple[str | int, ...] | None = None
    trust: str = 'trusted'


@dataclass(frozen=True)
class HostAction:
    """宿主自己的动作：`abort` 在 `key` 那个请求处，`interrupt` / `shutdown` 在 run 安静下来的
    那一处。`waiting` 是它发生时还在等外部世界的那些调用（模型调用 `('llm_call', agent id, 轮,
    第几次)`、工具调用的键、人的请求键）：给了它，回放只在恰好停在这些调用上时发出这个动作。"""

    kind: str
    key: tuple[str | int, ...] | None = None
    waiting: tuple[tuple[str | int, ...], ...] | None = None


@dataclass(frozen=True)
class DangerRuleSpec:
    id: str
    params: Mapping[str, str] = dataclasses.field(default_factory=_no_entries)


@dataclass(frozen=True)
class HostProfile:
    """宿主档案（v2）：回放据此重建 Runtime 的装配。没写的项是 None / 空。"""

    flags: Mapping[str, bool | int | str] = dataclasses.field(default_factory=_no_entries)
    danger_rules: tuple[DangerRuleSpec, ...] | None = None
    danger_table: Mapping[str, str] | None = None
    output_limit: int | None = None
    host_facts: HostFacts | None = None
    max_turns: int | None = None
    no_write: bool = False
    modules: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ScenarioSource:
    """捕获自哪里（v2）：trace id、命令行参数（已脱敏）、配置文件的 sha256、版本与 git commit。"""

    trace_id: str | None = None
    argv: tuple[str, ...] = ()
    version: str | None = None
    config_sha256: str | None = None
    git_commit: str | None = None


@dataclass(frozen=True)
class ScenarioExpectation:
    """`expect`：只比写出来的那几项（`specified`），各项逐字比较。v2 的 `events` 也可以按 Agent
    选择器分开写（`events_by_agent`）：几个 Agent 的事件交错的先后取决于调度。"""

    final_output: Mapping[str, str]
    tool_order: Mapping[str, tuple[str, ...]]
    stop_reasons: Mapping[str, str]
    errors: Mapping[str, str]
    statuses: Mapping[str, str]
    events: tuple[str, ...]
    workspace: Mapping[str, str]
    specified: frozenset[str]
    events_by_agent: Mapping[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class ScenarioDocument:
    version: int
    name: str
    roles: Mapping[str, ScenarioRole]
    topology: Topology | None
    entry: ScenarioEntry
    responses: tuple[ScenarioResponse, ...]
    expect: ScenarioExpectation
    source: str
    host: HostProfile | None = None
    host_actions: tuple[HostAction, ...] = ()
    ids: tuple[tuple[tuple[str | int, ...], str], ...] = ()
    origin: ScenarioSource | None = None


def _error(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _mapping(
    value: object,
    field: str,
    *,
    allowed: frozenset[str] | None = None,
    required: frozenset[str] = frozenset(),
    later: frozenset[str] = frozenset(),
) -> Mapping[str, object]:
    """`later` 是 v2 才有的键：在 v1 文档里出现时，下一步指向 `version: 2`。"""
    if not isinstance(value, Mapping):
        raise _error(
            f'{field} 是 Mapping',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            f'把 {field} 写成对象（YAML 映射或 JSON 对象）',
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
            newer = [key for key in unknown if key in later]
            raise _error(
                f'{field} 只含 {sorted(allowed)!r}',
                f'未知键 {unknown!r}',
                (
                    f'{newer!r} 是场景格式 v2 的键：把 version 改成 2，或删掉它们'
                    if newer and len(newer) == len(unknown)
                    else '删除未知键；场景字段写错时不能静默忽略'
                ),
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
            f'{field} 是列表',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            f'把 {field} 写成列表（YAML 的 `- ...` 或 JSON 数组）',
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


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _error(
            f'{field} 是字符串',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            f'把 {field} 写成带引号的字符串',
        )
    return value


def _optional_string(value: object, field: str) -> str | None:
    return None if value is None else _string(value, field)


def _integer(value: object, field: str, *, minimum: int) -> int:
    # `bool` 是 `int` 的子类，YAML 1.1 又把 yes / on / true 读成 True：不单独拒，`turn: yes`
    # 就成了 1。
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(
            f'{field} 是不小于 {minimum} 的整数',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            f'改成 {minimum} 或更大的整数；yes / on / true 会被 YAML 读成布尔值',
        )
    return value


def _optional_positive(value: object, field: str) -> int | None:
    return None if value is None else _integer(value, field, minimum=1)


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise _error(
            f'{field} 是 true 或 false',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            f'把 {field} 写成不带引号的 true / false',
        )
    return value


def _choice(value: object, field: str, choices: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise _error(
            f'{field} 是 {" / ".join(choices)} 之一',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            '改成其中之一，按字面逐字匹配（小写）',
        )
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise _error(
            f'{field} 是 64 个小写十六进制字符（sha256）',
            f'收到 {value!r}',
            '照 trace 的 payloads 目录里的文件名抄写；不要截断或改成大写',
        )
    return value


def _json_value(value: object, field: str, depth: int = 0) -> object:
    """JSON 表达得出的值，交回不可变的拷贝：列表成元组、映射成只读视图。嵌套深度与整份文档的节点
    数各有上限（`_MAX_DEPTH`、`_MAX_NODES`）：YAML 的锚点能造出成环或指数展开的结构。"""
    nodes = _NODES.get()
    if nodes is not None:
        nodes[0] += 1
        if nodes[0] > _MAX_NODES:
            raise _error(
                f'整份文档的 JSON 值不超过 {_MAX_NODES} 个节点',
                f'读到 {field} 时已经超过',
                '检查是不是有成倍展开的引用；捕获的文档不会这么大，手写的拆成几份',
            )
    if depth > _MAX_DEPTH:
        raise _error(
            f'{field} 嵌套不超过 {_MAX_DEPTH} 层',
            '嵌套更深（或者成环）',
            '把深层结构换成字符串；成环的 YAML 别名改成写出来的值',
        )
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise _error(
                f'{field} 只含 JSON 值',
                f'收到 {value!r}',
                '把 NaN 与无穷大换成字符串或 null：JSON 表达不了它们',
            )
        return value
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        bad = [key for key in raw if not isinstance(key, str)]
        if bad:
            raise _error(
                f'{field} 只含 JSON 值（对象的键是字符串）',
                f'非字符串键为 {bad!r}',
                '把这些键改成字符串：JSON 对象的键只能是字符串，数字键编码之后会撞上同名的字符串键',
            )
        return MappingProxyType(
            {
                cast(str, key): _json_value(item, f'{field}.{key}', depth + 1)
                for key, item in raw.items()
            }
        )
    if isinstance(value, list | tuple):
        items = cast(Sequence[object], value)
        return tuple(
            _json_value(item, f'{field}[{index}]', depth + 1) for index, item in enumerate(items)
        )
    raise _error(
        f'{field} 只含 JSON 值',
        f'收到 {type(value).__name__} 类型的 {value!r}',
        '把值改成字符串、数字、布尔值、null、列表或对象',
    )


def _json_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(
            f'{field} 是 Mapping',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            f'把 {field} 写成对象（YAML 映射或 JSON 对象）',
        )
    return cast(Mapping[str, object], _json_value(cast(object, value), field))


def _text(value: object, field: str, *, blank: bool = True) -> Text:
    """v2 的自由文本：字符串，或 `{payload: <sha256>}`（v1 的文本字段不经这里，只收字符串）。"""
    if isinstance(value, Mapping):
        data = _mapping(
            cast(object, value),
            field,
            allowed=frozenset({'payload'}),
            required=frozenset({'payload'}),
        )
        return PayloadText(_digest(data['payload'], f'{field}.payload'))
    if not isinstance(value, str) or (not blank and not value.strip()):
        expected = '非空白字符串' if not blank else '字符串'
        raise _error(
            f'{field} 是{expected}，或 `{{payload: <sha256>}}`',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            f'给 {field} 填写文本，或引用 trace 里的 payload',
        )
    return value


def _stable_key(value: object, field: str) -> tuple[str | int, ...]:
    """内核 `ids` 的稳定键：非空数组，第一项是出处的名字，每一项是字符串或整数。"""
    items = _sequence(value, field)
    if not items or not isinstance(items[0], str) or not items[0]:
        raise _error(
            f'{field} 是内核的稳定键：非空数组，第一项是出处的名字（字符串）',
            f'收到 {value!r}',
            "照捕获的文档原样抄写，例如 ['host', 'spawn', 0]",
        )
    for index, item in enumerate(items):
        if isinstance(item, bool) or not isinstance(item, str | int):
            raise _error(
                f'{field} 的每一项是字符串或整数',
                f'{field}[{index}] 是 {type(item).__name__} 类型的 {item!r}',
                '稳定键只由字符串与整数组成；true / false 不是整数',
            )
    return tuple(cast(Sequence[str | int], items))


def _call_position(value: object, field: str) -> tuple[int, int, int]:
    items = _sequence(value, field)
    if len(items) != 3:
        raise _error(
            f'{field} 是 [周期, 轮, 批内位置] 三个整数',
            f'收到 {value!r}',
            '周期与轮从 1 数起，批内位置从 0 数起',
        )
    cycle = _integer(items[0], f'{field}[0]', minimum=1)
    turn = _integer(items[1], f'{field}[1]', minimum=1)
    index = _integer(items[2], f'{field}[2]', minimum=0)
    return cycle, turn, index


def parse_scenario_document(raw: object, *, source: str) -> ScenarioDocument:
    """校验一份场景文档（v1 或 v2），交回不可变的 `ScenarioDocument`。`source` 是它从哪来的（文件
    路径），只进结果与错误。"""
    token = _NODES.set([0])
    try:
        return _document(raw, source)
    finally:
        _NODES.reset(token)


def _document(raw: object, source: str) -> ScenarioDocument:
    root = _mapping(raw, 'scenario')
    version = root.get('version')
    if type(version) is not int or version not in SCENARIO_VERSIONS:
        raise _error(
            'scenario.version 是整数 1 或 2',
            f'收到 version={version!r}，类型是 {type(version).__name__}',
            '写成 `version: 1`（手写脚本）或 `version: 2`（回放文档）；不要写 true、字符串或'
            '其他版本',
        )
    if version == 1:
        root = _mapping(root, 'scenario', allowed=_V1_ROOT, required=_V1_ROOT, later=_V2_ROOT)
    else:
        root = _mapping(root, 'scenario', allowed=_V2_ROOT, required=_V2_REQUIRED_ROOT)
    name = _nonblank(root['name'], 'scenario.name')
    roles = _roles(root['agents'], version)
    topology = _topology(root['topology'], version) if 'topology' in root else None
    entry = _entry(root['entry'], version)
    _check_entry(entry, topology, roles)
    responses = _responses(root.get('responses', ()), version)
    host = _host(root['host']) if 'host' in root else None
    host_actions = _host_actions(root.get('host_actions', ()))
    ids = _ids(root.get('ids', ()))
    origin = _origin(root['source']) if 'source' in root else None
    return ScenarioDocument(
        version=version,
        name=name,
        roles=MappingProxyType(roles),
        topology=topology,
        entry=entry,
        responses=responses,
        expect=_expectation(root['expect'], version),
        source=source,
        host=host,
        host_actions=host_actions,
        ids=ids,
        origin=origin,
    )


def _roles(value: object, version: int) -> dict[str, ScenarioRole]:
    mapping = _mapping(value, 'agents')
    if not mapping:
        raise _error(
            'agents 至少声明一个角色',
            '收到空 Mapping',
            '添加根角色及其 manifest / instances',
        )
    roles: dict[str, ScenarioRole] = {}
    call_ids: set[str] = set()
    keys: dict[tuple[str | int, ...], str] = {}
    for role, raw_role in mapping.items():
        role = _nonblank(role, 'agents 的角色名')
        field = f'agents.{role}'
        if version == 1:
            data = _mapping(raw_role, field, allowed=_V1_AGENT, required=_V1_AGENT, later=_V2_AGENT)
        else:
            data = _mapping(raw_role, field, allowed=_V2_AGENT, required=frozenset({'instances'}))
            if ('manifest' in data) == ('frozen' in data):
                raise _error(
                    f'{field} 恰好有 manifest 与 frozen 之一',
                    f'收到 {sorted(key for key in data if key in {"manifest", "frozen"})!r}',
                    '手写的角色用 manifest（交给目录加载）；捕获的冻结角色用 frozen',
                )
        manifest: Mapping[str, object] | None = None
        frozen: FrozenRole | None = None
        if 'manifest' in data:
            manifest = _manifest(data['manifest'], field)
        else:
            frozen = _frozen(data['frozen'], f'{field}.frozen')
        raw_instances = _sequence(data['instances'], f'{field}.instances')
        # v2 的角色可以一个实例都没有：捕获的文档列出宿主目录里的每个角色（回放要建得出它们，
        # 交给模型的工具定义里会点它们的名），没跑过的那些就没有实例。
        if not raw_instances and version == 1:
            raise _error(
                f'{field}.instances 至少有一份脚本',
                '收到空列表',
                '添加 `{turns: [...]}`；每个运行时实例消费一份独立脚本',
            )
        instances = tuple(
            _instance(f'{field}.instances[{index}]', item, version, call_ids, keys)
            for index, item in enumerate(raw_instances)
        )
        roles[role] = ScenarioRole(role, instances, manifest=manifest, frozen=frozen)
    total = sum(len(role.instances) for role in roles.values())
    if keys and len(keys) != total:
        raise _error(
            '每个实例都给稳定键 key，或者都不给',
            f'{total} 个实例里只有 {len(keys)} 个给了',
            '捕获的文档每个实例都有（回放按它认实例）；手写的都省略，按创建的先后消费',
        )
    return roles


def _manifest(value: object, field: str) -> Mapping[str, object]:
    data = _mapping(value, f'{field}.manifest')
    if data.get('impl') is not None:
        raise _error(
            'L2 场景 Agent 使用声明式 manifest，不加载任意 Python 类',
            f'{field}.manifest.impl={data.get("impl")!r}',
            '删除 impl；需要的行为用真实工具与 ScriptedLLM turns 表达',
        )
    # 只冻结、不校验：清单的字段由目录加载时校验。浅拷贝的话调用方手里那份列表一改，文档也跟着变。
    return cast(Mapping[str, object], _json_value(data, f'{field}.manifest'))


def _frozen(value: object, field: str) -> FrozenRole:
    data = _mapping(value, field, allowed=_FROZEN, required=frozenset({'system_prompt', 'tools'}))
    tools = _names(data['tools'], f'{field}.tools')
    impl = data.get('impl')
    if impl is not None and (not isinstance(impl, str) or impl not in FROZEN_IMPLS):
        raise _error(
            f'{field}.impl 是内核内置的类式角色之一：{sorted(FROZEN_IMPLS)!r}，或省略（声明式）',
            f'收到 {impl!r}',
            '回放文档里的 impl 不 import 任意路径；别的类式角色在回放里用不了，改成声明式重新捕获',
        )
    description = data.get('description')
    return FrozenRole(
        system_prompt=_text(data['system_prompt'], f'{field}.system_prompt'),
        tools=tools,
        max_turns=_optional_positive(data.get('max_turns'), f'{field}.max_turns'),
        impl=impl,
        model=_nonblank(data.get('model', 'default'), f'{field}.model'),
        description=None if description is None else _nonblank(description, f'{field}.description'),
    )


def _names(value: object, field: str) -> tuple[str, ...]:
    """有序、不重复的名字：顺序进工具定义（前缀缓存的一部分），重复的名字后一个会顶掉前一个。"""
    names: list[str] = []
    for index, item in enumerate(_sequence(value, field)):
        name = _nonblank(item, f'{field}[{index}]')
        if name in names:
            raise _error(
                f'{field} 里每个名字只出现一次',
                f'{name!r} 出现了不止一次（第 {index} 项）',
                '删掉重复的那一项；顺序按第一次出现的保留',
            )
        names.append(name)
    return tuple(names)


def _instance(
    field: str,
    value: object,
    version: int,
    call_ids: set[str],
    keys: dict[tuple[str | int, ...], str],
) -> ScenarioInstance:
    if version == 1:
        data = _mapping(
            value, field, allowed=_V1_INSTANCE, required=_V1_INSTANCE, later=_V2_INSTANCE
        )
    else:
        data = _mapping(value, field, allowed=_V2_INSTANCE, required=_V1_INSTANCE)
    key = None
    if 'key' in data:
        key = _stable_key(data['key'], f'{field}.key')
        if key in keys:
            raise _error(
                '每个实例的稳定键各不相同',
                f'{field}.key={list(key)!r} 与 {keys[key]}.key 相同',
                '一个稳定键只创建一个 Agent：照捕获的文档原样抄写，不要复制实例',
            )
        keys[key] = field
    replies = tuple(
        _reply(f'{field}.turns[{index}]', item, version, call_ids)
        for index, item in enumerate(_sequence(data['turns'], f'{field}.turns'))
    )
    _check_reply_keys(replies, field)
    outcomes = tuple(
        _outcome(f'{field}.tool_results[{index}]', item)
        for index, item in enumerate(
            _sequence(data.get('tool_results', ()), f'{field}.tool_results')
        )
    )
    skipped = tuple(
        _skipped(f'{field}.skipped[{index}]', item)
        for index, item in enumerate(_sequence(data.get('skipped', ()), f'{field}.skipped'))
    )
    gate = tuple(
        _gate(f'{field}.gate[{index}]', item)
        for index, item in enumerate(_sequence(data.get('gate', ()), f'{field}.gate'))
    )
    _unique_calls(field, 'tool_results', [item.call for item in outcomes], [])
    _unique_calls(
        field, 'skipped', [item.call for item in skipped], [item.call for item in outcomes]
    )
    _unique_calls(field, 'gate', [item.call for item in gate], [])
    if outcomes or skipped or gate:
        _check_positions(field, replies, outcomes, skipped, gate)
    return ScenarioInstance(replies, key=key, tool_results=outcomes, skipped=skipped, gate=gate)


def _check_positions(
    field: str,
    replies: tuple[ScenarioReply, ...],
    outcomes: tuple[ToolOutcome, ...],
    skipped: tuple[SkippedCall, ...],
    gate: tuple[GateRecord, ...],
) -> None:
    """按调用位置给的每一项都落在一次真有的调用上：那一轮最后一次模型调用交回的回复里第 `index`
    个工具调用。对不上的记录回放时永远消费不掉，到那时只剩一句「没用上」，指不回这里。"""
    if any(reply.turn is None for reply in replies):
        raise _error(
            f'{field} 有 tool_results / skipped / gate 时，turns 每一项都给 turn 与 attempt',
            'turns 没有按键给',
            '调用位置里的轮次要对得上回复：照捕获的文档给每一项写 turn 与 attempt',
        )
    calls: dict[int, int] = {}
    for reply in replies:
        calls[cast(int, reply.turn)] = len(reply.tool_calls)
    sections: tuple[tuple[str, tuple[ToolOutcome | SkippedCall | GateRecord, ...]], ...] = (
        ('tool_results', outcomes),
        ('skipped', skipped),
        ('gate', gate),
    )
    for section, items in sections:
        for index, item in enumerate(items):
            _, turn, position = item.call
            if position >= calls.get(turn, 0):
                raise _error(
                    f'{field}.{section} 的调用位置落在第 {turn} 轮回复里的工具调用上',
                    (
                        f'{field}.{section}[{index}].call={list(item.call)!r}，而第 {turn} 轮'
                        + (f'只有 {calls[turn]} 个工具调用' if turn in calls else '没有回复')
                    ),
                    '批内位置从 0 数起，按那一轮最后一次调用的回复数；照捕获的文档原样抄写',
                )


def _unique_calls(
    field: str, section: str, calls: list[tuple[int, int, int]], taken: list[tuple[int, int, int]]
) -> None:
    seen: set[tuple[int, int, int]] = set()
    for index, call in enumerate(calls):
        if call in seen:
            raise _error(
                f'{field}.{section} 里每个调用位置只出现一次',
                f'{field}.{section}[{index}].call={list(call)!r} 重复',
                '一次调用只有一个结局：删掉重复的那一项',
            )
        if call in taken:
            raise _error(
                f'{field} 里一次调用要么执行了（tool_results）、要么被跳过（skipped）',
                f'{field}.{section}[{index}].call={list(call)!r} 两边都有',
                '删掉不对的那一边：被 PRE_TOOL_USE 跳过的调用工具没有跑',
            )
        seen.add(call)


def _check_reply_keys(replies: tuple[ScenarioReply, ...], field: str) -> None:
    """「当前 LLM 调用」的键要么都给、要么都不给，给了就严格递增：回放按键取回复，同一个键两条回复、
    或缺了键的一条，都会让某一次调用拿到别人的回复。"""
    keyed = [reply for reply in replies if reply.turn is not None]
    if not keyed:
        return
    if len(keyed) != len(replies):
        raise _error(
            f'{field}.turns 每一项都给 turn 与 attempt，或者都不给',
            f'{len(replies)} 项里只有 {len(keyed)} 项给了',
            '捕获的文档每一项都有；手写的都省略，按先后消费',
        )
    previous: ScenarioReply | None = None
    for index, reply in enumerate(replies):
        current = (cast(int, reply.turn), cast(int, reply.attempt))
        before = (
            None if previous is None else (cast(int, previous.turn), cast(int, previous.attempt))
        )
        if before is not None and current <= before:
            raise _error(
                f'{field}.turns 的 (turn, attempt) 严格递增',
                f'第 {index} 项是 {list(current)!r}，前一项是 {list(before)!r}',
                '按调用发生的先后排列，同一轮的重试 attempt 依次加 1；不要重复',
            )
        expected = 1 if before is None or before[0] != current[0] else before[1] + 1
        if current[1] != expected:
            raise _error(
                f'{field}.turns 里每一轮的 attempt 从 1 起逐个加 1',
                f'第 {index} 项是 {list(current)!r}，这里应该是 attempt {expected}',
                '内核每一轮的首次调用是 1，重试与备用模型依次加 1：不要跳号',
            )
        if previous is not None and before is not None and before[0] == current[0]:
            if previous.raises is None:
                raise _error(
                    f'{field}.turns 里同一轮只有失败之后才有下一次调用',
                    f'第 {index} 项（{list(current)!r}）前面那次调用已经交回了回复',
                    '一轮里拿到回复就不再重试：删掉多余的那一项，或者把前一项改成 raises',
                )
        previous = reply


def _reply(field: str, value: object, version: int, call_ids: set[str]) -> ScenarioReply:
    if version == 1:
        data = _mapping(value, field, allowed=_V1_TURN, later=_V2_TURN)
    else:
        data = _mapping(value, field, allowed=_V2_TURN)
    calls = tuple(
        _call(f'{field}.tool_calls[{index}]', item, version, call_ids)
        for index, item in enumerate(_sequence(data.get('tool_calls', ()), f'{field}.tool_calls'))
    )
    turn = attempt = None
    if 'turn' in data or 'attempt' in data:
        if 'turn' not in data or 'attempt' not in data:
            raise _error(
                f'{field} 的 turn 与 attempt 一起给',
                f'只给了 {"turn" if "turn" in data else "attempt"}',
                'turn 是第几轮（从 1 数起），attempt 是这一轮的第几次调用（首次是 1，重试'
                '依次加 1）',
            )
        turn = _integer(data['turn'], f'{field}.turn', minimum=1)
        attempt = _integer(data['attempt'], f'{field}.attempt', minimum=1)
    if version >= 2 and 'raises' in data:
        others = sorted(key for key in ('content', 'tool_calls', 'extra', 'trust') if key in data)
        if others:
            raise _error(
                f'{field} 是一条回复，或者一次失败（raises），不是两者都有',
                f'raises 旁边还有 {others!r}',
                '模型调用失败时没有回复：只留 raises（以及 turn / attempt / request_digests）',
            )
    digests = None
    if 'request_digests' in data:
        digests = tuple(
            _digest(item, f'{field}.request_digests[{index}]')
            for index, item in enumerate(
                _sequence(data['request_digests'], f'{field}.request_digests')
            )
        )
    return ScenarioReply(
        content=_content(data.get('content'), f'{field}.content', version),
        tool_calls=calls,
        raises=_raised(data['raises'], f'{field}.raises', version) if 'raises' in data else None,
        turn=turn,
        attempt=attempt,
        extra=_json_object(data['extra'], f'{field}.extra') if 'extra' in data else None,
        trust=_choice(data.get('trust', 'trusted'), f'{field}.trust', _TRUST),
        request_digests=digests,
    )


def _content(value: object, field: str, version: int) -> Text | tuple[object, ...] | None:
    if value is None or isinstance(value, str):
        return value
    if version >= 2:
        if isinstance(value, Mapping):
            return _text(cast(object, value), field)
        if isinstance(value, list | tuple):
            return cast(tuple[object, ...], _json_value(cast(object, value), field))
    raise _error(
        f'{field} 是字符串或 null'
        + ('、内容块列表，或 `{payload: <sha256>}`' if version >= 2 else ''),
        f'收到 {value!r}，类型是 {type(value).__name__}',
        '改成模型会返回的文本，或省略',
    )


def _call(field: str, value: object, version: int, call_ids: set[str]) -> ScenarioCall:
    """模型给的一次工具调用。`arguments` 是解析后的参数（v1 只收对象，v2 任何 JSON 值），编码成交给
    工具的 JSON 文本；v2 里模型给的原串解析不了时写进 `arguments_raw`，原样交给工具——都写成字符串
    的话，原串 `ls` 与解析出来恰好是字符串的 `"ls"` 就分不开。"""
    if version == 1:
        data = _mapping(value, field, allowed=_V1_CALL, required=_V1_CALL, later=_V2_CALL)
    else:
        data = _mapping(value, field, allowed=_V2_CALL, required=frozenset({'id', 'name'}))
        if ('arguments' in data) == ('arguments_raw' in data):
            raise _error(
                f'{field} 恰好有 arguments 与 arguments_raw 之一',
                f'收到 {sorted(key for key in data if key.startswith("arguments"))!r}',
                'arguments 写解析后的参数；模型给的原串解析不了时写进 arguments_raw',
            )
    # 模型给的调用 id 只作记录：捕获的文档里它可以重复、可以是空串（内核的键不用它）。
    call_id = (
        _nonblank(data['id'], f'{field}.id') if version == 1 else _string(data['id'], f'{field}.id')
    )
    if version == 1:
        if call_id in call_ids:
            raise _error(
                '一个场景内每个 tool call id 只出现一次',
                f'{field}.id={call_id!r} 重复',
                '给每次调用一个稳定且唯一的 id；重复会让结果配对与确认答复指向错误调用',
            )
        call_ids.add(call_id)
    name = _nonblank(data['name'], f'{field}.name')
    if 'arguments_raw' in data:
        return ScenarioCall(call_id, name, _string(data['arguments_raw'], f'{field}.arguments_raw'))
    arguments = data['arguments']
    if version == 1:
        _mapping(arguments, f'{field}.arguments')
    _json_value(arguments, f'{field}.arguments')
    encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return ScenarioCall(call_id, name, encoded)


def _raised(value: object, field: str, version: int) -> ScenarioError:
    data = _mapping(value, field)
    if version >= 2 and 'type' not in data and 'kind' not in data:
        raise _error(
            f'{field} 是 {{type, message, status_code?, retry_after?}}'
            '（也收 v1 的 {kind, message}）',
            f'收到的键 {sorted(data)!r}',
            '照 trace 里错误的记录写 type（`模块.限定名`）与 message',
        )
    if version >= 2 and 'type' in data:
        data = _mapping(value, field, allowed=_V2_ERROR, required=frozenset({'type', 'message'}))
        kind = data['type']
        if not isinstance(kind, str) or not _QUALIFIED.fullmatch(kind):
            raise _error(
                f'{field}.type 是 `模块.限定名`',
                f'收到 {kind!r}',
                '照 trace 里错误的 type 抄写，例如 syndica.kernel.errors.LLMResponseError',
            )
        retry_after = data.get('retry_after')
        if retry_after is not None and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, int | float)
            or not math.isfinite(retry_after)
            or retry_after < 0
        ):
            raise _error(
                f'{field}.retry_after 是不小于 0 的有限秒数或 null',
                f'收到 {retry_after!r}，类型是 {type(retry_after).__name__}',
                '写成数字；没有就省略',
            )
        status = data.get('status_code')
        return ScenarioError(
            type=kind,
            message=_text(data['message'], f'{field}.message'),
            status_code=None
            if status is None
            else _integer(status, f'{field}.status_code', minimum=0),
            retry_after=None if retry_after is None else float(retry_after),
        )
    data = _mapping(
        value,
        field,
        allowed=_V1_ERROR,
        required=_V1_ERROR,
        later=_V2_ERROR if version == 1 else frozenset(),
    )
    kind = _nonblank(data['kind'], f'{field}.kind')
    message = _nonblank(data['message'], f'{field}.message')
    if kind not in _ERROR_KINDS:
        raise _error(
            f'{field}.kind 是 {sorted(_ERROR_KINDS)!r} 之一',
            f'收到 {kind!r}',
            '改成 runtime_error / context_overflow / llm_response_error；不接受 import path',
        )
    return ScenarioError(type=_ERROR_KINDS[kind], message=message)


def _output(value: object, field: str) -> Text | tuple[object, ...]:
    if isinstance(value, list | tuple):
        return cast(tuple[object, ...], _json_value(cast(object, value), field))
    return _text(value, field)


def _artifacts(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    artifacts: list[Mapping[str, object]] = []
    for index, item in enumerate(_sequence(value, field)):
        where = f'{field}[{index}]'
        data = _mapping(item, where, allowed=_ARTIFACT, required=_ARTIFACT - {'is_deliverable'})
        for name in sorted(_ARTIFACT - {'is_deliverable'}):
            _string(data[name], f'{where}.{name}')
        if 'is_deliverable' in data:
            _boolean(data['is_deliverable'], f'{where}.is_deliverable')
        artifacts.append(MappingProxyType(dict(data)))
    return tuple(artifacts)


def _outcome(field: str, value: object) -> ToolOutcome:
    data = _mapping(value, field, allowed=_OUTCOME, required=frozenset({'call'}))
    call = _call_position(data['call'], f'{field}.call')
    if 'raised' in data:
        extra = sorted(key for key in data if key not in {'call', 'raised', 'arguments_digest'})
        if extra:
            raise _error(
                f'{field} 是工具交回的结果，或它抛出的异常（raised），不是两者都有',
                f'raised 旁边还有 {extra!r}',
                '工具抛了就只写 call 与 raised',
            )
        return ToolOutcome(
            call=call,
            raised=_raised(data['raised'], f'{field}.raised', 2),
            arguments_digest=_arguments_digest(data, field),
        )
    if 'output' not in data:
        raise _error(
            f'{field} 有 output（工具交回的结果）或 raised（它抛出的异常）',
            f'两者都没有：{sorted(data)!r}',
            '照 trace 里这次调用的 tool.result 补上',
        )
    return ToolOutcome(
        call=call,
        output=_output(data['output'], f'{field}.output'),
        meta=_json_object(data.get('meta', {}), f'{field}.meta'),
        trust=_choice(data.get('trust', 'trusted'), f'{field}.trust', _TRUST),
        artifacts=_artifacts(data.get('artifacts', ()), f'{field}.artifacts'),
        arguments_digest=_arguments_digest(data, field),
    )


def _arguments_digest(data: Mapping[str, object], field: str) -> str | None:
    raw = data.get('arguments_digest')
    return None if raw is None else _digest(raw, f'{field}.arguments_digest')


def _skipped(field: str, value: object) -> SkippedCall:
    data = _mapping(value, field, allowed=_SKIPPED, required=frozenset({'call', 'output'}))
    return SkippedCall(
        call=_call_position(data['call'], f'{field}.call'),
        output=_output(data['output'], f'{field}.output'),
        meta=_json_object(data.get('meta', {}), f'{field}.meta'),
        trust=_choice(data.get('trust', 'trusted'), f'{field}.trust', _TRUST),
        artifacts=_artifacts(data.get('artifacts', ()), f'{field}.artifacts'),
    )


def _gate(field: str, value: object) -> GateRecord:
    data = _mapping(value, field, allowed=_GATE | _GATE_OPTIONAL, required=_GATE)
    rule_ids = tuple(
        _nonblank(item, f'{field}.rule_ids[{index}]')
        for index, item in enumerate(_sequence(data['rule_ids'], f'{field}.rule_ids'))
    )
    return GateRecord(
        call=_call_position(data['call'], f'{field}.call'),
        rule_ids=rule_ids,
        risk=_choice(data['risk'], f'{field}.risk', sorted(ATTRIBUTE_DOMAINS['danger.risk'])),
        provenance=_choice(
            data['provenance'],
            f'{field}.provenance',
            sorted(ATTRIBUTE_DOMAINS['danger.provenance']),
        ),
        decided_by=_choice(
            data['decided_by'],
            f'{field}.decided_by',
            sorted(ATTRIBUTE_DOMAINS['danger.decided_by']),
        ),
        disposition=_choice(
            data['disposition'],
            f'{field}.disposition',
            sorted(ATTRIBUTE_DOMAINS['danger.disposition']),
        ),
        outcome=(
            None
            if 'outcome' not in data
            else _choice(
                data['outcome'], f'{field}.outcome', sorted(ATTRIBUTE_DOMAINS['tool.outcome'])
            )
        ),
    )


def _topology(value: object, version: int) -> Topology:
    data = _mapping(value, 'topology', allowed=_TOPOLOGY, required=_TOPOLOGY)
    slots: list[AgentSlot] = []
    for index, raw_slot in enumerate(_sequence(data['slots'], 'topology.slots')):
        field = f'topology.slots[{index}]'
        slot = dict(_mapping(raw_slot, field, allowed=_SLOT))
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
        if isinstance(slot.get('extra_tools'), list):
            slot['extra_tools'] = tuple(cast(list[object], slot['extra_tools']))
        try:
            parsed = AgentSlot(**cast(Any, slot))
        except TypeError as exc:
            raise _error(
                f'{field} 至少含 name 与 agent',
                f'构造失败：{exc}',
                '补齐必填字段并删除未知字段',
            ) from exc
        if version == 1 and (
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
                    '把场景槽收窄到最小回放形态，或改用 version: 2；完整 topology 装配已有独立'
                    '集成测试，L2 v1 不静默忽略实例键或槽级增强'
                ),
            )
        slots.append(parsed)
    return Topology(slots=tuple(slots))


def _entry(value: object, version: int) -> ScenarioEntry:
    if version == 1:
        data = _mapping(value, 'entry', allowed=_V1_ENTRY, required=_V1_ENTRY, later=_V2_ENTRY)
        return ScenarioEntry(
            prompt=_nonblank(data['prompt'], 'entry.prompt'),
            slot=_nonblank(data['slot'], 'entry.slot'),
        )
    data = _mapping(value, 'entry', allowed=_V2_ENTRY, required=frozenset({'prompt'}))
    mode = _choice(data.get('mode', 'slot'), 'entry.mode', _ENTRY_MODES)
    needed, unwanted = ('slot', 'agent') if mode == 'slot' else ('agent', 'slot')
    if needed not in data or unwanted in data:
        raise _error(
            f'entry.mode={mode!r} 时给 entry.{needed}、不给 entry.{unwanted}',
            f'收到 {sorted(data)!r}',
            'slot 经拓扑槽开始；query / send 直接建 entry.agent 这个角色',
        )
    return ScenarioEntry(
        prompt=_text(data['prompt'], 'entry.prompt', blank=False),
        mode=mode,
        slot=_nonblank(data['slot'], 'entry.slot') if mode == 'slot' else None,
        agent=_nonblank(data['agent'], 'entry.agent') if mode != 'slot' else None,
        trust=_choice(data.get('trust', 'trusted'), 'entry.trust', _TRUST),
    )


def _check_entry(
    entry: ScenarioEntry, topology: Topology | None, roles: Mapping[str, ScenarioRole]
) -> None:
    if topology is not None:
        missing = sorted({slot.agent for slot in topology.slots} - set(roles))
        if missing:
            raise _error(
                'topology 引用的每个角色都在 scenario.agents 中提供 instances 脚本',
                f'缺少脚本的角色为 {missing!r}',
                '给每个角色添加 manifest 与至少一个 instances[].turns',
            )
    if entry.mode == 'slot':
        if topology is None:
            raise _error(
                'entry.mode 为 slot 时文档有 topology',
                '没有 topology',
                '补上 topology.slots，或把入口改成 entry.mode: query / send 直接建角色',
            )
        names = [slot.name for slot in topology.slots]
        if entry.slot not in names:
            raise _error(
                'entry.slot 是 topology.slots 中的槽名',
                f'收到 {entry.slot!r}；现有槽为 {names!r}',
                '把 entry.slot 改成现有槽名',
            )
    elif entry.agent not in roles:
        raise _error(
            'entry.agent 是 scenario.agents 里的角色名',
            f'收到 {entry.agent!r}；现有角色为 {sorted(roles)!r}',
            '把 entry.agent 改成现有角色名',
        )


def _responses(value: object, version: int) -> tuple[ScenarioResponse, ...]:
    responses: list[ScenarioResponse] = []
    keys: set[tuple[str | int, ...]] = set()
    forms: set[str] = set()
    for index, raw in enumerate(_sequence(value, 'responses')):
        field = f'responses[{index}]'
        keyed = version >= 2 and isinstance(raw, Mapping) and 'key' in raw
        forms.add('key' if keyed else 'agent')
        if len(forms) > 1:
            raise _error(
                'responses 全部按请求的稳定键（key）给，或者全部按先后（agent）给',
                f'{field} 与前面的写法不同',
                '捕获的文档按 key；手写的按 agent 选择器与先后，不要混用',
            )
        if keyed:
            response = _keyed_response(field, cast(object, raw))
            if response.key in keys:
                raise _error(
                    '每个请求只有一条答复',
                    f'{field}.key={list(cast(tuple[object, ...], response.key))!r} 重复',
                    '删掉重复的那一条',
                )
            keys.add(cast(tuple[str | int, ...], response.key))
        else:
            response = _ordered_response(field, cast(object, raw), version)
        responses.append(response)
    return tuple(responses)


def _keyed_response(field: str, value: object) -> ScenarioResponse:
    data = _mapping(value, field, allowed=_KEYED_RESPONSE, required=frozenset({'key', 'kind'}))
    kind = _choice(data['kind'], f'{field}.kind', _V2_RESPONSE_KINDS)
    if (kind == 'timeout') == ('answer' in data):
        raise _error(
            f'{field} 的 timeout 没有 answer，别的 kind 都有',
            f'kind={kind!r}，{"有" if "answer" in data else "没有"} answer',
            'timeout 表示人没答（问题过期）；答了就写 answer 与对应的 kind',
        )
    return ScenarioResponse(
        kind=kind,
        answer=_text(data['answer'], f'{field}.answer') if 'answer' in data else None,
        key=_stable_key(data['key'], f'{field}.key'),
        trust=_choice(data.get('trust', 'trusted'), f'{field}.trust', _TRUST),
    )


def _ordered_response(field: str, value: object, version: int) -> ScenarioResponse:
    data = _mapping(
        value,
        field,
        allowed=_V1_RESPONSE,
        required=frozenset({'kind', 'agent', 'answer'}),
        later=_KEYED_RESPONSE if version == 1 else frozenset(),
    )
    kind = _nonblank(data['kind'], f'{field}.kind')
    if kind not in _V1_RESPONSE_KINDS:
        raise _error(
            f'{field}.kind 是 {sorted(_V1_RESPONSE_KINDS)!r} 之一',
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
    return ScenarioResponse(
        kind=kind,
        agent=_nonblank(data['agent'], f'{field}.agent'),
        answer=_nonblank(data['answer'], f'{field}.answer'),
        tool_call_id=tool_call_id,
    )


def _host_actions(value: object) -> tuple[HostAction, ...]:
    actions: list[HostAction] = []
    aborted: set[tuple[str | int, ...]] = set()
    for index, raw in enumerate(_sequence(value, 'host_actions')):
        field = f'host_actions[{index}]'
        data = _mapping(raw, field, allowed=_HOST_ACTION, required=frozenset({'kind'}))
        kind = _choice(data['kind'], f'{field}.kind', _HOST_ACTION_KINDS)
        if (kind == 'abort') != ('key' in data):
            raise _error(
                f'{field}：abort 带它所在请求的稳定键，interrupt / shutdown 不带',
                f'kind={kind!r}，{"有" if "key" in data else "没有"} key',
                'interrupt 与 shutdown 发生在 run 安静下来的那一处，不需要位置',
            )
        key = _stable_key(data['key'], f'{field}.key') if kind == 'abort' else None
        waiting = _waiting(data, field, kind)
        if key is not None:
            if key in aborted:
                raise _error(
                    '每个请求至多被中止一次',
                    f'{field}.key={list(key)!r} 重复',
                    '删掉重复的那一条',
                )
            aborted.add(key)
        elif any(action.kind == kind for action in actions):
            raise _error(
                f'host_actions 里至多一条 {kind}',
                f'{field} 是第二条',
                '一次 run 只会被中断或关闭一次：删掉多余的那一条',
            )
        actions.append(HostAction(kind, key, waiting))
    return tuple(actions)


def _waiting(
    data: Mapping[str, object], field: str, kind: str
) -> tuple[tuple[str | int, ...], ...] | None:
    if 'waiting' not in data:
        return None
    if kind == 'abort':
        raise _error(
            f'{field}：waiting 只属于 interrupt / shutdown',
            'abort 带了 waiting',
            'abort 的位置就是它的 key（所在的请求），删掉 waiting',
        )
    keys: list[tuple[str | int, ...]] = []
    for index, raw in enumerate(_sequence(data['waiting'], f'{field}.waiting')):
        key = _stable_key(raw, f'{field}.waiting[{index}]')
        if key in keys:
            raise _error(
                f'{field}.waiting 里的键各不相同',
                f'{field}.waiting[{index}]={list(key)!r} 重复',
                '删掉重复的那一项',
            )
        keys.append(key)
    return tuple(keys)


def _ids(value: object) -> tuple[tuple[tuple[str | int, ...], str], ...]:
    pairs: list[tuple[tuple[str | int, ...], str]] = []
    keys: set[tuple[str | int, ...]] = set()
    ids: set[str] = set()
    for index, raw in enumerate(_sequence(value, 'ids')):
        field = f'ids[{index}]'
        data = _mapping(raw, field, allowed=_ID, required=_ID)
        key = _stable_key(data['key'], f'{field}.key')
        given = _nonblank(data['id'], f'{field}.id')
        if key in keys or given in ids:
            raise _error(
                'ids 里稳定键各不相同、id 各不相同',
                f'{field}（key={list(key)!r}，id={given!r}）与前面的重复',
                '一个稳定键只交出一个 id，注册表也拒绝重复的 id：删掉重复的那一项',
            )
        keys.add(key)
        ids.add(given)
        pairs.append((key, given))
    return tuple(pairs)


def _host(value: object) -> HostProfile:
    data = _mapping(value, 'host', allowed=_HOST)
    flags: dict[str, bool | int | str] = {}
    for key, item in _mapping(data.get('flags', {}), 'host.flags').items():
        if isinstance(item, float) or not isinstance(item, bool | int | str):
            raise _error(
                f'host.flags.{key} 是布尔值、整数或字符串',
                f'收到 {item!r}，类型是 {type(item).__name__}',
                'Flag 的取值只有这三种类型；照 `syndica flags show <key>` 的默认值写',
            )
        flags[key] = item
    rules = table = None
    if 'danger' in data:
        danger = _mapping(data['danger'], 'host.danger', allowed=_DANGER)
        if 'rules' in danger:
            rules = _danger_rules(danger['rules'])
        if 'table' in danger:
            table = {
                _rule_id(key): _choice(item, f'host.danger.table.{key}', _DISPOSITIONS)
                for key, item in _mapping(danger['table'], 'host.danger.table').items()
            }
    facts = None
    if 'host_facts' in data:
        raw_facts = _mapping(
            data['host_facts'], 'host.host_facts', allowed=_HOST_FACTS, required=_HOST_FACTS
        )
        facts = HostFacts(
            os=_nonblank(raw_facts['os'], 'host.host_facts.os'),
            platform=_nonblank(raw_facts['platform'], 'host.host_facts.platform'),
            shell=_nonblank(raw_facts['shell'], 'host.host_facts.shell'),
        )
    modules = _names(data['modules'], 'host.modules') if 'modules' in data else None
    return HostProfile(
        flags=MappingProxyType(flags),
        danger_rules=rules,
        danger_table=None if table is None else MappingProxyType(table),
        output_limit=_optional_positive(data.get('output_limit'), 'host.output_limit'),
        host_facts=facts,
        max_turns=_optional_positive(data.get('max_turns'), 'host.max_turns'),
        no_write=_boolean(data.get('no_write', False), 'host.no_write'),
        modules=modules,
    )


def _rule_id(key: str) -> str:
    """处置表的键按字面逐字匹配规则 ID：带首尾空白的键永远匹配不上。"""
    if not key.strip() or key != key.strip():
        raise _error(
            'host.danger.table 的键是不带首尾空白的非空规则 ID',
            f'收到键 {key!r}',
            "照 DangerVerdict.rule_id 原样写，例如 'shell.privilege_escalation'",
        )
    return key


def _danger_rules(value: object) -> tuple[DangerRuleSpec, ...]:
    """有序：判定取第一个命中的规则，顺序变了结论就可能变。"""
    rules: list[DangerRuleSpec] = []
    for index, raw in enumerate(_sequence(value, 'host.danger.rules')):
        field = f'host.danger.rules[{index}]'
        data = _mapping(raw, field, allowed=_RULE, required=frozenset({'id'}))
        rule_id = _choice(data['id'], f'{field}.id', sorted(RULE_PARAMETERS))
        if any(rule.id == rule_id for rule in rules):
            raise _error(
                'host.danger.rules 里每条规则只出现一次',
                f'{field}.id={rule_id!r} 重复',
                '删掉后一条：判定取第一个命中的规则，后一条永远轮不到',
            )
        params = _mapping(data.get('params', {}), f'{field}.params')
        allowed = RULE_PARAMETERS[rule_id]
        unknown = sorted(key for key in params if key not in allowed)
        required = _REQUIRED_RULE_PARAMETERS & allowed
        missing = sorted(required - set(params))
        if unknown or missing:
            raise _error(
                f'{field}.params 只含 {sorted(allowed)!r}，且含 {sorted(required)!r}',
                f'未知 {unknown!r}，缺少 {missing!r}',
                'root 是工作区根的字面值，real 是记录时解析出的路径；没有参数的规则不写 params',
            )
        rules.append(
            DangerRuleSpec(
                rule_id,
                MappingProxyType(
                    {key: _nonblank(item, f'{field}.params.{key}') for key, item in params.items()}
                ),
            )
        )
    return tuple(rules)


def _origin(value: object) -> ScenarioSource:
    data = _mapping(value, 'source', allowed=_SOURCE)
    trace_id = data.get('trace_id')
    if trace_id is not None and (
        not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id)
    ):
        raise _error(
            'source.trace_id 是 32 个小写十六进制字符',
            f'收到 {trace_id!r}',
            '照 `syndica trace list` 的 id 列抄写完整的 id',
        )
    config = data.get('config_sha256')
    argv = tuple(
        _string(item, f'source.argv[{index}]')
        for index, item in enumerate(_sequence(data.get('argv', ()), 'source.argv'))
    )
    return ScenarioSource(
        trace_id=trace_id,
        argv=argv,
        version=_optional_string(data.get('version'), 'source.version'),
        config_sha256=None if config is None else _digest(config, 'source.config_sha256'),
        git_commit=_optional_string(data.get('git_commit'), 'source.git_commit'),
    )


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


def _expectation(value: object, version: int) -> ScenarioExpectation:
    data = _mapping(value, 'expect', allowed=_EXPECT)
    raw_events = data.get('events', ())
    by_agent: dict[str, tuple[str, ...]] | None = None
    if version >= 2 and isinstance(raw_events, Mapping):
        by_agent = {}
        for selector, sequence in cast(Mapping[object, object], raw_events).items():
            name = _nonblank(selector, 'expect.events 的键')
            by_agent[name] = _event_kinds(sequence, f'expect.events.{name}')
        raw_events = ()
    events = _event_kinds(raw_events, 'expect.events')
    return ScenarioExpectation(
        final_output=_string_map(data.get('final_output', {}), 'expect.final_output'),
        tool_order=_tool_order(data.get('tool_order', {})),
        stop_reasons=_string_map(data.get('stop_reasons', {}), 'expect.stop_reasons'),
        errors=_string_map(data.get('errors', {}), 'expect.errors'),
        statuses=_string_map(data.get('statuses', {}), 'expect.statuses'),
        events=events,
        workspace=_string_map(data.get('workspace', {}), 'expect.workspace'),
        specified=frozenset(data),
        events_by_agent=None if by_agent is None else MappingProxyType(by_agent),
    )


def _event_kinds(value: object, field: str) -> tuple[str, ...]:
    return tuple(
        _nonblank(item, f'{field}[{index}]') for index, item in enumerate(_sequence(value, field))
    )
