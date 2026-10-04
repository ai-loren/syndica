"""Kernel: runtime, agent, tools, hooks, protocols.

零重依赖：只允许标准库 + pydantic + 一个 HTTP 客户端。
不读环境变量：路径 / 凭证 / 开关由宿主注入。

`__all__` 是公开 API：列进去就是永久兼容承诺，宁少勿多，不够再加（spec §4.2）。

**判据是「已经公开的签名需不需要它」**，不是「它有没有用」：一个名字进这里，当且仅当宿主拿不到
它就用不了某个已经公开的入口——`Runtime.wait_for(waiter)` 造不出 `Waiter`、`AgentOptions` 的
`danger_policy` 字段填不出 `DangerPolicy`、`Runtime(plugins=...)` 写不出 `Plugin` 标注。校验函数、
内核内部的收口函数、以及只有内核在生产的对象（门、执行器、实例表）都不在这里：加一个公开名字不是
破坏性变更，去掉一个是。
"""

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.agents import Orchestrator, register_builtins
from syndica.kernel.catalog import AgentCatalog, HostFacts, OverrideRecord
from syndica.kernel.context import (
    ArtifactRef,
    Context,
    ContextBlock,
    Role,
    StopReason,
    ToolCall,
    TrustLevel,
    provenance_of,
    strictest_trust,
)
from syndica.kernel.danger import (
    DEFAULT_RULES,
    NO_RULE_MATCHED,
    AllowAll,
    BlockHighRisk,
    ConfirmationHandler,
    ConfirmationOutcome,
    ConfirmationRequest,
    DangerJudge,
    DangerPolicy,
    DangerRequest,
    DangerRule,
    DangerVerdict,
    Disposition,
    MailboxConfirmationHandler,
    RiskLevel,
)
from syndica.kernel.errors import (
    ConfigError,
    ContractViolation,
    LLMResponseError,
    PayloadRefused,
    SyndicaError,
    ToolFatalError,
)
from syndica.kernel.events import Event, EventBus, Subscription
from syndica.kernel.hooks import (
    ExtensionJunction,
    HookCallback,
    HookResult,
    HooksCenter,
    Junction,
    JunctionKind,
    JunctionRef,
)
from syndica.kernel.ids import IdSource, StableKey
from syndica.kernel.isolation import LocalIsolator, MergeConflict, MergeReport
from syndica.kernel.llm import (
    LLM,
    CachePrefixProvider,
    ContextOverflowError,
    LLMConfig,
    ScriptedLLM,
    ScriptedTurn,
)
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.manifest import AgentManifest, ManifestSource
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.modules import (
    FlagDeclaration,
    FlagRow,
    FlagValue,
    ModuleHost,
    RuntimeModule,
    SafetyModule,
    flag_reference,
)
from syndica.kernel.plugins import Plugin
from syndica.kernel.prefix_drift import PrefixDriftModule, PrefixDriftRecord, PrefixDriftService
from syndica.kernel.protocols import AgentRegistry, MessageBroker, MissingQueueCallback, TraceStore
from syndica.kernel.redaction import (
    Finding,
    RedactedText,
    RedactionRule,
    Redactor,
    RuleRedactor,
    encode_redacted,
)
from syndica.kernel.retry import RetryModule
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.scenario_schema import (
    PayloadText,
    ScenarioDocument,
    parse_scenario_document,
)
from syndica.kernel.stores import (
    FileTraceStore,
    InMemoryAgentRegistry,
    InMemoryMessageBroker,
    InMemoryTraceStore,
)
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools import (
    AskUser,
    AskUserQuestion,
    HumanInteractionModule,
    orchestration_tools,
)
from syndica.kernel.tools.local import local_tools
from syndica.kernel.topology import AgentSlot, Topology
from syndica.kernel.trace import (
    AttributeValue,
    CausalityEdge,
    EdgeKind,
    PayloadRef,
    Span,
    SpanEvent,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
    TraceSource,
    verbatim_attribute,
)
from syndica.kernel.trace_recorder import TraceRecorder
from syndica.kernel.tracing import NullTracer, Tracer
from syndica.kernel.versions import VersionEntry, VersionStore
from syndica.kernel.waiting import (
    Confirmation,
    Reply,
    UserInput,
    Waiter,
    WaitResult,
    WaitState,
)
from syndica.kernel.workspace import (
    ExecResult,
    FileStat,
    Portal,
    PtyCapture,
    PtyStatus,
    Workspace,
    WriteConflict,
)
from syndica.kernel.workspace_router import WorkspaceRouter

__all__ = [
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
    'ExtensionJunction',
    'FileStat',
    'FileTraceStore',
    'Finding',
    'FlagDeclaration',
    'FlagRow',
    'FlagValue',
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
    'LocalIsolator',
    'LocalWorkspace',
    'MailboxConfirmationHandler',
    'ManifestSource',
    'MergeConflict',
    'MergeReport',
    'Message',
    'MessageBroker',
    'MissingQueueCallback',
    'ModuleHost',
    'NO_RULE_MATCHED',
    'NullTracer',
    'Orchestrator',
    'OverrideRecord',
    'PayloadRef',
    'PayloadRefused',
    'PayloadText',
    'Plugin',
    'Portal',
    'PrefixDriftModule',
    'PrefixDriftRecord',
    'PrefixDriftService',
    'PtyCapture',
    'PtyStatus',
    'RedactedText',
    'RedactionRule',
    'Redactor',
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
    'VersionEntry',
    'VersionStore',
    'WaitResult',
    'WaitState',
    'Waiter',
    'Workspace',
    'WorkspaceRouter',
    'WriteConflict',
    'encode_redacted',
    'flag_reference',
    'local_tools',
    'orchestration_tools',
    'parse_scenario_document',
    'provenance_of',
    'register_builtins',
    'strictest_trust',
    'verbatim_attribute',
]
