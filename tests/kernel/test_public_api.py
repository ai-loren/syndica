"""`syndica.kernel.__all__` 是公开 API：列进去就是永久兼容承诺，导不进来就是发布即坏。"""

from __future__ import annotations

import ast
import dataclasses
import os
from pathlib import Path

import syndica.kernel as kernel
import syndica.kernel.contracts as contracts
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM
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
    'InMemoryAgentRegistry',
    'InMemoryMessageBroker',
    'Junction',
    'JunctionKind',
    'JunctionRef',
    'LLM',
    'LLMConfig',
    'LLMResponseError',
    'Message',
    'MessageBroker',
    'MissingQueueCallback',
    'NO_RULE_MATCHED',
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
    'Trigger',
    'TrustLevel',
    'UserInput',
    'WaitResult',
    'WaitState',
    'Waiter',
    'orchestration_tools',
    'provenance_of',
    'strictest_trust',
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
# `BaseAgent` 的公开面。`kernel.__all__` 只导出类名这一个字符串，对类上新增的方法完全无感——
# 一次「顺手加个公开方法」因此在这里之外没有任何东西会变红。
_BASE_AGENT_SURFACE: tuple[str, ...] = (
    'agent_id',
    'attach_gate',
    'context',
    'get_system_prompt',
    'install_runtime_tools',
    'options',
    'register_hook',
    'run',
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
#   `ControlBlock`：控制面自己的接缝，`Runtime` 是唯一的生产者、`BaseAgent` 是唯一的消费者，
#   宿主不传 gate 时默认就是 `NullTurnGate`。
# - 校验函数（`validate_*`、`tool_name_disagreement`）：收下配置的那一层自己调，宿主拿它没有用。
# - `ToolExecutor` / `DEFAULT_MAX_TOOL_CONCURRENCY`：执行器由 `BaseAgent` 构造，上限的默认值经
#   `AgentOptions` 的字段默认值就取得到。
# - 五个编排工具类：`orchestration_tools` 是它们唯一的工厂，交回的是 `dict[str, Tool]`，
#   而 `Tool` 已经在公开面上；宿主要装一个子集按键挑即可，不需要类。
# - `install_plugins`：`Runtime(plugins=...)` 是它唯一的公开入口。
# - `INTERCEPTABLE` / `OBSERVABLE` / `ReadOnlyKwarg`：分类表是内核自己分发时用的，
#   `ReadOnlyKwarg` 是 `dict` 子类、hook 作者按 `dict` 用它。
# - `LocalAgentTable`：spec §4.3 明文说它只在进程内有意义、不进 Protocol。
# - `UPDATE_*`：第三方实现 `AgentRegistry.update` 要用，但它们不出现在任何公开签名里，
#   落点是随 wheel 发布的契约套件（见 D-53）。
# - `correlation_of`：`Waiter` 密封分发的内部收口，`wait_for` 自己调。
_WITHHELD: tuple[str, ...] = (
    'BrokerTurnGate',
    'ControlBlock',
    'DEFAULT_MAX_TOOL_CONCURRENCY',
    'Delegate',
    'Fork',
    'INTERCEPTABLE',
    'KillAgent',
    'LocalAgentTable',
    'NullTurnGate',
    'OBSERVABLE',
    'QueryAgent',
    'ReadOnlyKwarg',
    'SendMessage',
    'ToolExecutor',
    'TurnGate',
    'TurnGateDecision',
    'UPDATE_PROTECTED_FIELDS',
    'UPDATE_PROTECTED_FIELD_OWNERS',
    'UPDATE_WRITABLE_FIELDS',
    'correlation_of',
    'disposition_of',
    'install_plugins',
    'tool_name_disagreement',
    'validate_agent_factories',
    'validate_danger_options',
    'validate_max_concurrency',
    'validate_tool_names',
)


def _declared_exports() -> tuple[dict[str, set[str]], list[str]]:
    """({名字: 声明它的子模块}, 本次真的扫过的文件)。

    期望值来自**文件树**，不是包自己的 re-export 面：拿 `dir(kernel)` 或任何一个 `__init__`
    去推，漏掉一个子模块的名字会让期望值跟着缩水。
    """
    declared: dict[str, set[str]] = {}
    scanned: list[str] = []
    for path in sorted(_PACKAGE.rglob('*.py')):
        relative = path.relative_to(_PACKAGE).as_posix()
        scanned.append(relative)
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == '__all__' for target in node.targets
            ):
                continue
            if not isinstance(node.value, ast.List):
                continue
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    declared.setdefault(element.value, set()).add(relative)
    return declared, scanned


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


def test_the_base_agent_surface_is_exactly_this():
    """同样按实例：`agent_id` / `options` 是 `__init__` 里赋的，而宿主读的正是它们。"""
    assert sorted(name for name in dir(_agent()) if not name.startswith('_')) == sorted(
        _BASE_AGENT_SURFACE
    )


def test_the_agent_options_surface_is_exactly_this():
    options = AgentOptions(name='surface', llm=ScriptedLLM([]))
    assert sorted(name for name in dir(options) if not name.startswith('_')) == sorted(
        _AGENT_OPTIONS_SURFACE
    )


def test_the_agent_options_field_order_is_exactly_this():
    """新字段只能追加在末尾：插在中间会让每一个按位置传参的调用方静默改变含义。"""
    assert [field.name for field in dataclasses.fields(AgentOptions)] == list(_AGENT_OPTIONS_FIELDS)


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


def test_no_name_is_both_promised_and_withheld():
    """两份清单必须互斥：一个名字同时出现在两边时，上一条会因为差集少一项而变红，而读者看到的
    却是「它在扣留清单里」——把这条矛盾单独判出来，才不用靠另一条断言的算术去发现它。"""
    assert sorted(set(_WITHHELD) & set(kernel.__all__)) == []
