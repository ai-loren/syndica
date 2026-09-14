"""`syndica.kernel.__all__` 是公开 API：列进去就是永久兼容承诺，导不进来就是发布即坏。"""

from __future__ import annotations

import syndica.kernel as kernel
import syndica.kernel.contracts as contracts
from syndica.kernel.runtime import Runtime

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
    'broker',
    'despawn',
    'ensure_active',
    'events',
    'find',
    'hooks',
    'kill',
    'pause',
    'plugins',
    'registry',
    'resume',
    'send',
    'shutdown',
    'spawn',
    'wait',
    'wait_for',
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


def test_the_runtime_surface_is_exactly_this():
    """按实例枚举而不是按类：`registry` / `broker` / `events` / `hooks` 是 `__init__` 里赋的
    实例属性，只看 `dir(Runtime)` 会漏掉它们，而宿主读写的正是这四个。
    """
    runtime = Runtime()
    assert sorted(name for name in dir(runtime) if not name.startswith('_')) == sorted(
        _RUNTIME_SURFACE
    )
