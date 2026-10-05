"""人的反馈升级为持久标准（§7.7）：自动提议、人确认后生效。

反馈天生自带锚点——就是这句话本身，常还带产物位置（「这张表第 3 列口径不对」）。所以它可以升级
为一条 Rubric，进此后每一轮验收，同样的错不犯第二次。

两道闸：

- **只认人发的**（评审 H11）：别的 Agent 经 `SendMessage` 发来的消息带着它的 agent id（内核盖章、
  消息内容伪造不了），被注入的 Agent 不能借反馈给自己出题。`is_human_feedback` 就是这条判断。
- **提议 ≠ 生效**：提议进 RubricStore 是 `proposed`、默认不进验收，人 `activate` 后才生效。不把随口
  一句固化成永久标准（§7.7「自动提议，人确认后生效」）。

提议器只读这条反馈本身与它带出的产物定位，不读整份轨迹或工作区（无 Portal）。它的模型调用失败时退回
「原话即标准」并按 `judgment` 记：拿不准时宁可要人判，也不让一条反馈被自动判通过。信任随反馈。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Mapping
from typing import Any, cast

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Rubric,
    RubricKind,
    RubricOrigin,
    RubricStatus,
)
from syndica.kernel.acceptance_inject import verify_anchors
from syndica.kernel.companion import run_companion
from syndica.kernel.context import TrustLevel
from syndica.kernel.llm import LLM
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.redaction import Redactor
from syndica.kernel.rubric_redaction import redacted_rubric

__all__ = ['is_human_feedback', 'propose_feedback_rubric']

logger = logging.getLogger(__name__)

_PROMPT = (
    '你把人对产出的一条反馈改写成一条可验收的标准。只依据这条反馈本身，不要补充它没说的要求。'
    'kind：反馈要求的是可检验的具体事实（口径、字段、格式）就是 checkable；是整体观感（结论太弱、'
    '不够易读）就是 judgment。只输出一个 JSON 对象：{"statement": "...", "kind": "checkable"}'
)


def is_human_feedback(sender: str, agent_ids: Collection[str]) -> bool:
    """这条消息是不是人发的：发送方不是任何一个 Agent。`SendMessage` 由内核盖上发送 Agent 的 id，
    消息内容改不了它，所以 Agent 冒充不了人（评审 H11）。"""
    return sender not in agent_ids


async def propose_feedback_rubric(
    *,
    scope: str,
    rubric_id: str,
    feedback: str,
    locator: str,
    store: RubricStore,
    redactor: Redactor,
    artifacts: Mapping[str, str] | None = None,
    llm: LLM | None = None,
    trust: TrustLevel = TrustLevel.TRUSTED,
    max_turns: int = 4,
) -> Rubric | None:
    """把一条人的反馈提议为 `proposed` Rubric 存进 store，交回它；锚点不可重开时不提议、交回 None。

    锚点 = 反馈消息本身（`locator` → `feedback`）+ 它带出的产物定位（`artifacts`：locator → 原文）。
    """
    sources = {locator: feedback, **(artifacts or {})}
    statement, kind = await _phrase(feedback, llm, trust, max_turns)
    anchors = (Anchor(AnchorSource.USER_MESSAGE, locator, feedback),) + tuple(
        Anchor(AnchorSource.ARTIFACT, where, quote) for where, quote in (artifacts or {}).items()
    )
    if verify_anchors(anchors, sources):
        logger.warning(
            '期望: 反馈的锚点能重新打开。 实际: Agent %s 收到的反馈 %s 锚不回来源。 '
            '下一步: 不提议这一条。',
            scope,
            rubric_id,
        )
        return None
    rubric = Rubric(
        id=rubric_id,
        statement=statement,
        kind=kind,
        anchors=anchors,
        status=RubricStatus.PROPOSED,
        trust=trust,
        origin=RubricOrigin.FEEDBACK,
    )
    await store.propose(scope, redacted_rubric(rubric, redactor))
    return rubric


async def _phrase(
    feedback: str, llm: LLM | None, trust: TrustLevel, max_turns: int
) -> tuple[str, RubricKind]:
    """把反馈改写成标准句与类别。没有模型或模型失败 → 原话即标准、按 judgment（拿不准就要人判）。"""
    if llm is None:
        return feedback, RubricKind.JUDGMENT
    try:
        result = await run_companion(
            name='FeedbackProposer',
            llm=llm,
            system_prompt=_PROMPT,
            task=feedback,
            portal=None,
            max_turns=max_turns,
            trust=trust,
        )
        text = result.reply.strip()
        data = json.loads(text[text.find('{') : text.rfind('}') + 1])
    except Exception:
        return feedback, RubricKind.JUDGMENT
    if not isinstance(data, dict):
        return feedback, RubricKind.JUDGMENT
    parsed = cast('dict[str, Any]', data)
    statement = parsed.get('statement')
    kind = parsed.get('kind')
    if not isinstance(statement, str) or not statement.strip():
        return feedback, RubricKind.JUDGMENT
    return statement, RubricKind.CHECKABLE if kind == 'checkable' else RubricKind.JUDGMENT
