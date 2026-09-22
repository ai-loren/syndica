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
    DangerJudge,
    DangerPolicy,
    DangerRequest,
    DangerRule,
    DangerVerdict,
    Disposition,
    RiskLevel,
)
from syndica.kernel.errors import (
    ConfigError,
    ContractViolation,
    LLMResponseError,
    SyndicaError,
    ToolFatalError,
)
from syndica.kernel.events import Event, EventBus, Subscription
from syndica.kernel.hooks import (
    HookCallback,
    HookResult,
    HooksCenter,
    Junction,
    JunctionKind,
    JunctionRef,
)
from syndica.kernel.llm import LLM, ContextOverflowError, LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.manifest import AgentManifest, ManifestSource
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.plugins import Plugin
from syndica.kernel.protocols import AgentRegistry, MessageBroker, MissingQueueCallback
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools import orchestration_tools
from syndica.kernel.topology import AgentSlot, Topology
from syndica.kernel.waiting import (
    Confirmation,
    Reply,
    UserInput,
    Waiter,
    WaitResult,
    WaitState,
)

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
    'BaseAgent',
    'BlockHighRisk',
    'ConfigError',
    'Confirmation',
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
    'Event',
    'EventBus',
    'ExitMode',
    'HookCallback',
    'HookResult',
    'HooksCenter',
    'HostFacts',
    'InMemoryAgentRegistry',
    'InMemoryMessageBroker',
    'Junction',
    'JunctionKind',
    'JunctionRef',
    'LLM',
    'LLMConfig',
    'LLMResponseError',
    'ManifestSource',
    'Message',
    'MessageBroker',
    'MissingQueueCallback',
    'NO_RULE_MATCHED',
    'Orchestrator',
    'OverrideRecord',
    'Plugin',
    'Reply',
    'RiskLevel',
    'Role',
    'Runtime',
    'ScriptedLLM',
    'ScriptedTurn',
    'StopReason',
    'Subscription',
    'SyndicaError',
    'Tool',
    'ToolCall',
    'ToolFatalError',
    'ToolResult',
    'Topology',
    'Trigger',
    'TrustLevel',
    'UserInput',
    'WaitResult',
    'WaitState',
    'Waiter',
    'orchestration_tools',
    'provenance_of',
    'register_builtins',
    'strictest_trust',
]
