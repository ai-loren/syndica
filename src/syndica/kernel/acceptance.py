"""验收的内核类型与四值裁决推导（spec §7.5 / §7.6；§17 #22 / #45）。

内核只持有**类型**与**四值语义**：Goal / Anchor / Rubric / 裁决。验收策略（提示词、裁决口径、重试）
在验收 Runtime Module 里，换一套范式不动内核（§7.5 归属）。

两条语义**由内核强制、不交给策略 / LLM**，因为它们是 #45 的定义而非验收口径（评审 B3 / B4）：

- `judgment` 类 Rubric 的结论**恒为 `NEEDS_HUMAN`**，不论验收器报什么——它只能由人判（§7.6）。
- 裁决状态**由 per-rubric 结论推导**，不采信验收器自报：没有可检验标准、或验收器自身失败时落
  `UNVERIFIED`（fail-open 放行但显式标记，§10 / §13），**绝不塌缩成 `PASS`**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from syndica.kernel.context import TrustLevel, strictest_trust
from syndica.kernel.errors import ContractViolation

__all__ = [
    'Anchor',
    'AnchorSource',
    'Goal',
    'OutcomeKind',
    'Rubric',
    'RubricKind',
    'RubricOutcome',
    'RubricStatus',
    'Verdict',
    'VerdictStatus',
    'derive_verdict',
    'rubric_trust',
    'unfinished_verdict',
]


class AnchorSource(Enum):
    """锚点指向哪里。取值逐字落盘，改一个即破坏性变更。"""

    USER_MESSAGE = 'user_message'
    FILE = 'file'
    ARTIFACT = 'artifact'


class RubricKind(Enum):
    CHECKABLE = 'checkable'
    JUDGMENT = 'judgment'


class RubricStatus(Enum):
    ACTIVE = 'active'
    PROPOSED = 'proposed'


class OutcomeKind(Enum):
    """一条 Rubric 的验收结论。

    `UNMET` 与 `UNCONFIRMED` 必须分开：前者「有证据表明做错了」（→ `BLOCK`），后者「没能确认」
    （→ `PARTIAL`）。spec §7.5 区分二者是因为重试口径不同；混同会把「没验证」当「已证伪」或反过来。
    """

    SATISFIED = 'satisfied'
    UNMET = 'unmet'
    UNCONFIRMED = 'unconfirmed'
    NEEDS_HUMAN = 'needs_human'


class VerdictStatus(Enum):
    PASS = 'pass'
    PARTIAL = 'partial'
    BLOCK = 'block'
    UNVERIFIED = 'unverified'


@dataclass(frozen=True)
class Anchor:
    """一条可重新打开的引用链一环（spec §7.6）。锚点规则的牙齿是「能重新打开」：定位可解析、
    `quote` 确实出现在该位置——这由 RubricsBuilder 的校验在铸造处强制，不是这个 dataclass 的事。"""

    source: AnchorSource
    locator: str
    quote: str = ''


@dataclass(frozen=True)
class Goal:
    """唯一权威（spec §7.6）：自然语言 intent（允许含不可检验要求）+ 指回用户原话 / 任务说明的锚点。
    Rubric 是它的可检验投影，Goal 变更即重新投影。"""

    intent: str
    anchors: tuple[Anchor, ...] = ()


@dataclass(frozen=True)
class Rubric:
    """Goal 的一条可检验投影（spec §7.6）。`anchors` 非空：锚不回 Goal 的要求不得写入（防验收方发明
    需求）。`trust` 取投影输入里最严的——从不可信任务输入投出来的 Rubric 仍不可信（§12.2）。"""

    id: str
    statement: str
    kind: RubricKind
    anchors: tuple[Anchor, ...]
    mandatory: bool = True
    status: RubricStatus = RubricStatus.ACTIVE
    trust: TrustLevel = TrustLevel.TRUSTED

    def __post_init__(self) -> None:
        if not self.anchors:
            raise ContractViolation(
                expected='每条 Rubric 带非空锚点链，可重新打开追回 Goal（spec §7.6）',
                received=f'Rubric {self.id!r} 的 anchors 为空',
                next_action='给出指向用户原话/任务说明/产物的锚点；锚不回 Goal 的别写成 Rubric',
            )


@dataclass(frozen=True)
class RubricOutcome:
    """一条 Rubric 的验收结论。`UNMET` 必须带证据——没有证据的「不满足」是 `UNCONFIRMED`，不是
    `UNMET`，否则「没验证」会被当成「已证伪」（spec §7.5）。"""

    rubric_id: str
    kind: OutcomeKind
    evidence: tuple[Anchor, ...] = ()

    def __post_init__(self) -> None:
        if self.kind is OutcomeKind.UNMET and not self.evidence:
            raise ContractViolation(
                expected='UNMET 结论带证明「做错了」的证据锚点（否则它是 UNCONFIRMED，spec §7.5）',
                received=f'Rubric {self.rubric_id!r} 报 UNMET 但没有证据',
                next_action='只是没确认就用 UNCONFIRMED（→ partial），别用 UNMET（→ block）',
            )


@dataclass(frozen=True)
class Verdict:
    """L3 的裁决（spec §7.5 三态 + §10 / §13 的 `unverified` = 四值）。

    **只由 `derive_verdict` 造**：`status` 从 per-rubric 结论推导、不收验收器自报，`judgment` 条强制
    `NEEDS_HUMAN`。直接 `Verdict(status=PASS, ...)` 绕过推导是 bug（评审 B4）。
    """

    status: VerdictStatus
    outcomes: tuple[RubricOutcome, ...]
    needs_human: tuple[str, ...]
    reason: str = ''
    unverified_cause: str | None = None
    trust: TrustLevel = TrustLevel.TRUSTED


def derive_verdict(
    rubrics: Sequence[Rubric],
    reported: Mapping[str, RubricOutcome],
    *,
    reason: str = '',
    trust: TrustLevel = TrustLevel.TRUSTED,
    failure: str | None = None,
) -> Verdict:
    """从验收器报上来的 per-rubric 结论推导四值裁决（内核强制 #45 语义，评审 B3 / B4）。

    `rubrics` 只取 `ACTIVE` 的。`judgment` 条无视 `reported`、恒 `NEEDS_HUMAN`；`checkable` 条取
    `reported` 结论（报 `NEEDS_HUMAN` 这种它判不了的 → 降 `UNCONFIRMED`；没报 → `UNCONFIRMED`）。

    状态：验收器自身失败（`failure` 非空）→ `UNVERIFIED`；没有 active checkable 条 → `UNVERIFIED`
    （空集合不是「全满足」，评审 B3）；任一 checkable `UNMET`（带证据）→ `BLOCK`；还有 `UNCONFIRMED`
    → `PARTIAL`；checkable 全 `SATISFIED` → `PASS`。`needs_human` 不进状态、不触发重试，但带出来。
    """
    active = [r for r in rubrics if r.status is RubricStatus.ACTIVE]
    outcomes: list[RubricOutcome] = []
    needs_human: list[str] = []
    for rubric in active:
        if rubric.kind is RubricKind.JUDGMENT:
            outcomes.append(RubricOutcome(rubric.id, OutcomeKind.NEEDS_HUMAN))
            needs_human.append(rubric.id)
            continue
        got = reported.get(rubric.id)
        if got is None or got.kind is OutcomeKind.NEEDS_HUMAN:
            outcomes.append(RubricOutcome(rubric.id, OutcomeKind.UNCONFIRMED))
        else:
            outcomes.append(RubricOutcome(rubric.id, got.kind, got.evidence))

    checkable = [o for o in outcomes if o.kind is not OutcomeKind.NEEDS_HUMAN]
    status, cause = _derive_status(checkable, failure)
    return Verdict(
        status=status,
        outcomes=tuple(outcomes),
        needs_human=tuple(needs_human),
        reason=reason,
        unverified_cause=cause,
        trust=trust,
    )


def _derive_status(
    checkable: Sequence[RubricOutcome], failure: str | None
) -> tuple[VerdictStatus, str | None]:
    if failure is not None:
        return VerdictStatus.UNVERIFIED, failure
    if not checkable:
        return VerdictStatus.UNVERIFIED, 'no_active_rubrics'
    if any(o.kind is OutcomeKind.UNMET for o in checkable):
        return VerdictStatus.BLOCK, None
    if any(o.kind is OutcomeKind.UNCONFIRMED for o in checkable):
        return VerdictStatus.PARTIAL, None
    return VerdictStatus.PASS, None


def unfinished_verdict(
    rubrics: Sequence[Rubric], *, reason: str = '', trust: TrustLevel = TrustLevel.TRUSTED
) -> Verdict:
    """L2 输出验收判「没做完」时的裁决：恒 `PARTIAL`（§7.5：未完成 → partial）。

    这是一次**降级**——由 L2 的二值结论确定地映射，不收验收器自报的状态，永远不会是 `PASS`，所以不
    违反「状态由内核推导」。各 checkable 条记 `UNCONFIRMED`（L2 不逐条判）、judgment 条照旧
    `NEEDS_HUMAN`。没有 Rubric 时也给 `PARTIAL`：L2 不靠 Rubric 判「要求做完没有」。"""
    active = [r for r in rubrics if r.status is RubricStatus.ACTIVE]
    outcomes: list[RubricOutcome] = []
    needs_human: list[str] = []
    for rubric in active:
        if rubric.kind is RubricKind.JUDGMENT:
            outcomes.append(RubricOutcome(rubric.id, OutcomeKind.NEEDS_HUMAN))
            needs_human.append(rubric.id)
        else:
            outcomes.append(RubricOutcome(rubric.id, OutcomeKind.UNCONFIRMED))
    return Verdict(
        status=VerdictStatus.PARTIAL,
        outcomes=tuple(outcomes),
        needs_human=tuple(needs_human),
        reason=reason,
        trust=trust,
    )


def rubric_trust(inputs: Sequence[TrustLevel]) -> TrustLevel:
    """一条投影出的 Rubric 的信任 = 投影输入里最严的（§12.2：不可信输入投出的 Rubric 仍不可信）。"""
    trust = TrustLevel.TRUSTED
    for level in inputs:
        trust = strictest_trust(trust, level)
    return trust
