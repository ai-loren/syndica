"""回放文档里只有宿主知道的部分（M3 T14）：宿主档案与冻结后的角色。

写法同场景格式 v2，交给 `TraceRecorder.describe_run`；脱敏由 recorder 在写文档时统一做。值从 run
真正用的那些对象上读（危险判定器的规则与参数、策略的表、冻结出的 Agent 的选项），不在这里另写一份：
两份迟早不一致，回放就按另一套规则判。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from syndica.kernel import (
    AgentCatalog,
    AgentFactory,
    BaseAgent,
    EventBus,
    HooksCenter,
    HostFacts,
    RuntimeModule,
)
from syndica.kernel.danger import DangerJudge, RuleTablePolicy

__all__ = ['frozen_roles', 'host_profile']


def host_profile(
    *,
    flags: Mapping[str, bool | int | str],
    judge: DangerJudge,
    policy: RuleTablePolicy,
    output_limit: int,
    facts: HostFacts,
    max_turns: int | None,
    no_write: bool,
    modules: Sequence[RuntimeModule],
) -> dict[str, object]:
    return {
        'flags': dict(flags),
        'danger': {
            'rules': [{'id': rule.rule_id, 'params': dict(rule.params)} for rule in judge.rules],
            'table': {rule_id: value.value for rule_id, value in policy.table.items()},
        },
        'output_limit': output_limit,
        'host_facts': {'os': facts.os, 'platform': facts.platform, 'shell': facts.shell},
        'max_turns': max_turns,
        'no_write': no_write,
        'modules': [module.name for module in modules],
    }


def frozen_roles(
    catalog: AgentCatalog, factories: Mapping[str, AgentFactory]
) -> dict[str, dict[str, object]]:
    """每个角色冻结之后的样子：建一个 Agent 读它的选项（系统提示词是插值之后的正文、工具按顺序）。
    编排工具不在其中：它们由 Runtime 在 spawn 时装，回放的 Runtime 同样会装。"""
    roles: dict[str, dict[str, object]] = {}
    for name in catalog.names():
        agent = factories[name](HooksCenter(), EventBus())
        options = agent.options
        cls = type(agent)
        manifest = catalog.get(name)
        roles[name] = {
            'system_prompt': options.system_prompt,
            'tools': list(options.tools),
            'max_turns': options.max_turns,
            'impl': None if cls is BaseAgent else f'{cls.__module__}:{cls.__qualname__}',
            'model': manifest.model,
            'description': manifest.description,
        }
    return roles
