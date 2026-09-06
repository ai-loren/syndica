"""Kernel: runtime, agent, tools, hooks, protocols.

零重依赖：只允许标准库 + pydantic + 一个 HTTP 客户端。
不读环境变量：路径 / 凭证 / 开关由宿主注入。

`__all__` 是公开 API：列进去就是永久兼容承诺，宁少勿多，不够再加。
"""

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import (
    ArtifactRef,
    Context,
    ContextBlock,
    StopReason,
    ToolCall,
    TrustLevel,
)
from syndica.kernel.errors import (
    ConfigError,
    ContractViolation,
    LLMResponseError,
    SyndicaError,
    ToolFatalError,
)
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLM, ContextOverflowError, LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MessageBroker
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult

__all__ = [
    'AgentFactory',
    'AgentInfo',
    'AgentOptions',
    'AgentRegistry',
    'AgentStatus',
    'ArtifactRef',
    'BaseAgent',
    'ConfigError',
    'Context',
    'ContextBlock',
    'ContextOverflowError',
    'ContractViolation',
    'DeliveryResult',
    'Event',
    'EventBus',
    'ExitMode',
    'HookResult',
    'HooksCenter',
    'InMemoryAgentRegistry',
    'InMemoryMessageBroker',
    'Junction',
    'LLM',
    'LLMConfig',
    'LLMResponseError',
    'Message',
    'MessageBroker',
    'Runtime',
    'ScriptedLLM',
    'ScriptedTurn',
    'StopReason',
    'SyndicaError',
    'Tool',
    'ToolCall',
    'ToolFatalError',
    'ToolResult',
    'Trigger',
    'TrustLevel',
]
