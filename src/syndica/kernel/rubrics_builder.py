"""RubricsBuilder（§7.6）：开工之前把 Goal 投影成带锚点的 Rubric，持久化进 RubricStore。

经伴生运行器跑，**无 Portal、无文件工具**：它只拿到「被要求了什么」（Goal + 任务输入渲染进 task），
结构上读不到「产出了什么」——这条「看不到产物」由受限接口保证、不靠提示词自觉（硬约束 6）。

锚点规则是唯一的闸（§7.6）：每条 Rubric 必须带能**重新打开**的锚点（locator 能在 sources 里解析、
quote 确实出现在那处）。锚不回的候选丢弃并记一条 WARNING（主体 / 丢弃数 / 原因），不静默吞。投影出的
Rubric 信任取投影输入里最严的（§12.2：不可信任务输入投出的 Rubric 仍不可信）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any, cast

from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind, RubricStatus
from syndica.kernel.acceptance_inject import verify_anchors
from syndica.kernel.companion import run_companion
from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ContractViolation
from syndica.kernel.llm import LLM
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.redaction import Redactor
from syndica.kernel.rubric_redaction import redacted_rubric

__all__ = ['project_rubrics']

logger = logging.getLogger(__name__)

_SOURCES = {m.value: m for m in AnchorSource}
_KINDS = {m.value: m for m in RubricKind}

_SYSTEM_PROMPT = (
    '你是验收标准（Rubric）投影器。你只看得到「被要求了什么」（任务目标与任务输入），看不到、也不要'
    '猜测「产出了什么」。把目标投影成一组可验收的标准，每条**必须**带一个能重新打开的锚点：指向下面'
    '给出的某个来源（locator）并引用其中原文（quote）。锚不回来源的要求不要写成标准——那是在发明需求。\n'
    'kind 两类：checkable（可自动或由模型检验）、judgment（只能由人判断，输出「需人工确认」）。\n'
    '只输出一个 JSON 对象，形如：\n'
    '{"rubrics": [{"id": "R1", "statement": "...", "kind": "checkable", "mandatory": true, '
    '"anchors": [{"source": "user_message", "locator": "msg:1", "quote": "原文片段"}]}]}'
)


async def project_rubrics(
    *,
    scope: str,
    goal: Goal,
    sources: Mapping[str, str],
    llm: LLM,
    store: RubricStore,
    redactor: Redactor,
    trust: TrustLevel = TrustLevel.TRUSTED,
    max_turns: int = 6,
    time_limit: float | None = None,
) -> list[Rubric]:
    """把 Goal 投影成 Rubric、**替换**进 store 里投影出的那批，返回留下的那些（锚点可重开的）。

    投影失败（模型错、超时、回复解析不了）只接 `Exception`、按一条也没投出来处理：store 里旧 Goal 投
    影出的条照样退役——拿旧要求去验新 Goal 会判出不属于它的 PASS（评审 H7）；Rubric 为空时裁决落
    `unverified`。取消照常上抛。"""
    try:
        async with asyncio.timeout(time_limit):
            result = await run_companion(
                name='RubricsBuilder',
                llm=llm,
                system_prompt=_SYSTEM_PROMPT,
                task=_render(goal, sources),
                portal=None,
                max_turns=max_turns,
                trust=trust,
            )
        candidates = _parse(result.reply, trust)
    except Exception as exc:
        logger.warning(
            '期望: RubricsBuilder 投出 Rubric。 实际: Agent %s 的 Goal 投影失败（%s）。 '
            '下一步: 退役旧的投影条，本次验收落 unverified。',
            scope,
            type(exc).__name__,
        )
        candidates = []
    kept: list[Rubric] = []
    dropped = 0
    for rubric in candidates:
        problems = verify_anchors(rubric.anchors, sources)
        if problems:
            dropped += 1
            continue
        if any(earlier.id == rubric.id for earlier in kept):
            # 同一个 id 投了两条：store 按 id 存，后一条会静默顶掉前一条。留先投的那条，说一声。
            logger.warning(
                '期望: 每条 Rubric 的 id 互不相同。 实际: Agent %s 的投影里 %s 出现了不止一次。 '
                '下一步: 只留第一条；查 RubricsBuilder 的回复。',
                scope,
                rubric.id,
            )
            continue
        kept.append(rubric)
    if dropped:
        logger.warning(
            '期望: 每条 Rubric 锚回 Goal。 实际: Agent %s 的 %d 条锚点不可重新打开、已丢弃。 '
            '下一步: 这些是发明的需求，不进验收；查 Goal 与任务输入是否对得上。',
            scope,
            dropped,
        )
    await store.put_rubrics(scope, [redacted_rubric(r, redactor) for r in kept])
    return kept


def _render(goal: Goal, sources: Mapping[str, str]) -> str:
    lines = [f'任务目标：{goal.intent}', '', '可引用的来源（locator —— 原文）：']
    for locator, text in sources.items():
        lines.append(f'- {locator}：{text}')
    return '\n'.join(lines)


def _parse(reply: str, trust: TrustLevel) -> list[Rubric]:
    data = _load(reply)
    if data is None:
        return []
    raw = data.get('rubrics')
    if not isinstance(raw, list):
        return []
    out: list[Rubric] = []
    for item in cast('list[object]', raw):
        rubric = _rubric_from(item, trust)
        if rubric is not None:
            out.append(rubric)
    return out


def _load(reply: str) -> dict[str, object] | None:
    text = reply.strip()
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        logger.warning(
            '期望: RubricsBuilder 回 JSON 对象。 实际: 回复里没有 JSON。 下一步: 本次不投影。'
        )
        return None
    try:
        value = json.loads(text[start : end + 1])
    except (ValueError, RecursionError):
        logger.warning('期望: RubricsBuilder 回合法 JSON。 实际: 解析失败。 下一步: 本次不投影。')
        return None
    return cast('dict[str, object]', value) if isinstance(value, dict) else None


def _rubric_from(item: object, trust: TrustLevel) -> Rubric | None:
    if not isinstance(item, dict):
        return None
    data = cast('dict[str, Any]', item)
    rid = data.get('id')
    statement = data.get('statement')
    raw_kind = data.get('kind')
    kind = _KINDS.get(raw_kind) if isinstance(raw_kind, str) else None
    if not isinstance(rid, str) or not isinstance(statement, str) or kind is None:
        return None
    anchors = _anchors_from(data.get('anchors'))
    if not anchors:
        return None  # 没有锚点的候选直接弃（锚点规则，§7.6）
    try:
        return Rubric(
            id=rid,
            statement=statement,
            kind=kind,
            anchors=anchors,
            mandatory=data.get('mandatory') is not False,
            status=RubricStatus.ACTIVE,
            trust=trust,
        )
    except ContractViolation:
        return None  # 模型给的 id / statement 不合规：弃这一条，不让整次投影崩


def _anchors_from(raw: object) -> tuple[Anchor, ...]:
    if not isinstance(raw, list):
        return ()
    anchors: list[Anchor] = []
    for entry in cast('list[object]', raw):
        if not isinstance(entry, dict):
            continue
        item = cast('dict[str, Any]', entry)
        raw_source = item.get('source')
        source = _SOURCES.get(raw_source) if isinstance(raw_source, str) else None
        locator = item.get('locator')
        quote = item.get('quote', '')
        if source is None or not isinstance(locator, str) or not isinstance(quote, str):
            continue
        anchors.append(Anchor(source=source, locator=locator, quote=quote))
    return tuple(anchors)
