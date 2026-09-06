"""`syndica.kernel.__all__` 是公开 API：列进去就是永久兼容承诺，导不进来就是发布即坏。"""

from __future__ import annotations

import syndica.kernel as kernel
import syndica.kernel.contracts as contracts

# 逐字写死，**不从 `kernel.__all__` 推导**：删一个导出、加一个导出都必须先在这里变红。用被测对象
# 自己的导出面算出来的期望值（「每个名字都解析得到」「列表有序」）两条都由构造恒成立，测的是恒等式
# 而不是这条承诺。这份清单同时钉住数量、拼写、无重复与落盘顺序。
_KERNEL_EXPORTS: tuple[str, ...] = (
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
)
# contracts 随 wheel 发布，是第三方写 Store 实现的入口（spec §13.3），同属兼容承诺。
_CONTRACT_EXPORTS: tuple[str, ...] = (
    'AgentRegistryContract',
    'ContractSuite',
    'MessageBrokerContract',
)


def test_every_exported_name_resolves():
    assert [name for name in kernel.__all__ if not hasattr(kernel, name)] == []


def test_export_list_is_sorted():
    assert list(kernel.__all__) == sorted(kernel.__all__)


def test_the_public_surface_is_exactly_this():
    assert list(kernel.__all__) == list(_KERNEL_EXPORTS)


def test_the_contract_suite_surface_is_exactly_this():
    assert list(contracts.__all__) == list(_CONTRACT_EXPORTS)
    assert [name for name in contracts.__all__ if not hasattr(contracts, name)] == []
