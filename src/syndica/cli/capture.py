"""回放文档里只有宿主知道的部分：宿主档案与冻结后的角色。

写法同场景格式 v2，交给 `TraceRecorder.describe_run`；脱敏由 recorder 在写文档时统一做。危险判定器
的规则与参数、策略、冻结出的 Agent 的选项从 run 真正用的那些对象上读；Runtime 的装配（编排、确认
通道的超时）、输出上限与轮数由宿主传进来，宿主构造 Runtime 用的是同一份值。两份迟早不一致，回放就
按另一套装配跑。
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
from syndica.kernel.danger import (
    AllowAll,
    BlockHighRisk,
    DangerJudge,
    DangerPolicy,
    RuleTablePolicy,
)

__all__ = ['frozen_roles', 'host_profile']


def host_profile(
    *,
    flags: Mapping[str, bool | int | str],
    judge: DangerJudge,
    policy: DangerPolicy,
    output_limit: int,
    facts: HostFacts,
    max_turns: int | None,
    no_write: bool,
    modules: Sequence[RuntimeModule],
    orchestration: bool,
    confirmation_timeout: float | None,
    roles: Mapping[str, DangerPolicy] | None = None,
) -> dict[str, object]:
    danger: dict[str, object] = {
        'rules': [{'id': rule.rule_id, 'params': dict(rule.params)} for rule in judge.rules],
        **_policy(policy),
    }
    if roles:
        danger['roles'] = {role: _policy(role_policy) for role, role_policy in roles.items()}
    return {
        'flags': dict(flags),
        'danger': danger,
        'output_limit': output_limit,
        'host_facts': {'os': facts.os, 'platform': facts.platform, 'shell': facts.shell},
        'max_turns': max_turns,
        'no_write': no_write,
        'modules': [module.name for module in modules],
        'orchestration': orchestration,
        'confirmation_timeout': confirmation_timeout,
    }


def _policy(policy: DangerPolicy) -> dict[str, object]:
    """策略的种类（回放按它重建）。别的策略写下它的类名：场景格式不认，`describe_run` 当场报出
    这个 run 回放不出来。"""
    if isinstance(policy, RuleTablePolicy):
        return {
            'policy': 'table',
            'table': {rule_id: value.value for rule_id, value in policy.table.items()},
        }
    if isinstance(policy, AllowAll):
        return {'policy': 'allow_all'}
    if isinstance(policy, BlockHighRisk):
        return {'policy': 'block_high_risk'}
    kind = type(policy)
    return {'policy': f'{kind.__module__}:{kind.__qualname__}'}


def frozen_roles(
    catalog: AgentCatalog, factories: Mapping[str, AgentFactory], default: DangerPolicy
) -> tuple[dict[str, dict[str, object]], dict[str, DangerPolicy]]:
    """每个角色冻结之后的样子，以及处置不是 `default` 那个对象的角色各自的策略。都从建出来的
    Agent 上读（系统提示词是插值之后的正文、工具按顺序）：`--no-write` 重新冻结过，读真正交给
    Agent 的那一份才不会记错。每个角色只建一次——第三方的构造函数不该为了记录多跑一遍。编排工具
    不在其中：它们由 Runtime 在 spawn 时装，回放的 Runtime 同样会装。"""
    roles: dict[str, dict[str, object]] = {}
    policies: dict[str, DangerPolicy] = {}
    for name in catalog.names():
        agent = factories[name](HooksCenter(), EventBus())
        options = agent.options
        if options.danger_policy is not None and options.danger_policy is not default:
            policies[name] = options.danger_policy
        cls = type(agent)
        manifest = catalog.get(name)
        roles[name] = {
            'system_prompt': options.system_prompt,
            'tools': list(options.tools),
            'max_turns': options.max_turns,
            'impl': None if cls is BaseAgent else f'{cls.__module__}:{cls.__qualname__}',
            'model': manifest.model,
            'description': manifest.description,
            **(
                {'artifact_kinds': list(manifest.artifact_kinds)} if manifest.artifact_kinds else {}
            ),
        }
    return roles, policies
