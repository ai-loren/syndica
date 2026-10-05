"""L2 / L3 验收器与 COMPLETION_PROPOSED hook（§7.5）：裁决内核推导、L2 保守、fail-open→unverified、
超时用 asyncio.timeout、重试有上限、取消照常上抛。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Rubric,
    RubricKind,
    VerdictStatus,
)
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ContextBlock, StopReason, TrustLevel
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.rubric_redaction import redacted_rubric
from syndica.kernel.stores.memory_rubric import InMemoryRubricStore
from syndica.kernel.verifiers import acceptance_hook, verify_outcome, verify_output

_A = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'x'),)
_R1 = Rubric(id='R1', statement='has section 4', kind=RubricKind.CHECKABLE, anchors=_A)
_J1 = Rubric(id='J1', statement='reads well', kind=RubricKind.JUDGMENT, anchors=_A)


def _llm(*replies: object) -> ScriptedLLM:
    return ScriptedLLM([ScriptedTurn(content=json.dumps(r)) for r in replies])


def _outcome(result: str, *, evidence: bool = False) -> dict[str, object]:
    entry: dict[str, object] = {'rubric_id': 'R1', 'result': result}
    if evidence:
        entry['evidence'] = [{'source': 'file', 'locator': 'report.md', 'quote': 'no §4'}]
    return {'outcomes': [entry], 'reason': 'checked'}


async def _outcome_verdict(reply: object, rubrics: list[Rubric] | None = None):
    return await verify_outcome(
        llm=_llm(reply),
        rubrics=rubrics if rubrics is not None else [_R1],
        transcript='t',
        portal=None,
        max_turns=4,
        time_limit=5.0,
        trust=TrustLevel.TRUSTED,
    )


async def test_outcome_satisfied_is_pass() -> None:
    assert (await _outcome_verdict(_outcome('satisfied'))).status is VerdictStatus.PASS


async def test_outcome_unmet_with_evidence_is_block() -> None:
    assert (await _outcome_verdict(_outcome('unmet', evidence=True))).status is VerdictStatus.BLOCK


async def test_outcome_unmet_without_evidence_is_only_partial() -> None:
    # 没证据的 unmet 不是「已证伪」，只是「没能确认」（§7.5）。
    assert (await _outcome_verdict(_outcome('unmet'))).status is VerdictStatus.PARTIAL


async def test_outcome_cannot_pass_a_judgment_rubric() -> None:
    reply = {'outcomes': [{'rubric_id': 'J1', 'result': 'satisfied'}], 'reason': 'r'}
    verdict = await _outcome_verdict(reply, [_J1])
    assert verdict.needs_human == ('J1',)
    assert verdict.status is VerdictStatus.UNVERIFIED  # 只有 judgment 条，没有可检验的


async def test_outcome_with_unparseable_reply_raises() -> None:
    with pytest.raises(ValueError):
        await verify_outcome(
            llm=ScriptedLLM([ScriptedTurn(content='no json here')]),
            rubrics=[_R1],
            transcript='t',
            portal=None,
            max_turns=4,
            time_limit=5.0,
            trust=TrustLevel.TRUSTED,
        )


async def _output(reply: object):
    return await verify_output(
        llm=_llm(reply),
        transcript='t',
        portal=None,
        max_turns=4,
        time_limit=5.0,
        trust=TrustLevel.TRUSTED,
    )


async def test_output_complete_true_is_complete() -> None:
    assert (await _output({'complete': True, 'reason': 'all done'})).complete is True


async def test_output_is_conservative_about_anything_but_a_literal_true() -> None:
    # L2 保守：不是字面量 true 一律判没做完（§7.5），"yes" / 1 都不算。
    assert (await _output({'complete': 'yes'})).complete is False
    assert (await _output({'complete': 1})).complete is False
    assert (await _output({'complete': False, 'reason': 'missing §4'})).complete is False


class _SlowLLM(ScriptedLLM):
    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        await asyncio.sleep(10)
        return await super().create(messages, **kwargs)


async def _seeded_store(*rubrics: Rubric) -> InMemoryRubricStore:
    store = InMemoryRubricStore()
    await store.put_rubrics('s1', [redacted_rubric(r, RuleRedactor()) for r in rubrics])
    return store


def _main(turns: int, *, max_turns: int = 50) -> tuple[BaseAgent, HooksCenter]:
    hooks = HooksCenter()
    script = [ScriptedTurn(content=f'done {i}') for i in range(turns)]
    options = AgentOptions(
        name='Worker', llm=ScriptedLLM(script), tools={}, system_prompt='SP', max_turns=max_turns
    )
    return BaseAgent(options, hooks=hooks, events=EventBus()), hooks


def _hook(store, llm, *, output=False, outcome=True, partial=1, block=2, timeout=5.0):  # type: ignore[no-untyped-def]
    return acceptance_hook(
        store=store,
        scope='s1',
        llm=llm,
        portal=None,
        output=output,
        outcome=outcome,
        partial_retries=partial,
        block_retries=block,
        max_turns=4,
        timeout=timeout,
    )


async def test_the_hook_passes_a_satisfied_rubric_and_completes() -> None:
    agent, hooks = _main(1)
    hooks.register(
        Junction.COMPLETION_PROPOSED, _hook(await _seeded_store(_R1), _llm(_outcome('satisfied')))
    )
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert agent._verdict is not None and agent._verdict.status is VerdictStatus.PASS  # pyright: ignore[reportPrivateUsage]


async def test_l2_unfinished_sends_back_then_l3_passes() -> None:
    # 第一次判完成：L2 说没做完 → partial 打回（不跑 L3）；第二次：L2 说做完了 → L3 通过。
    agent, hooks = _main(2)
    verifier = _llm(
        {'complete': False, 'reason': 'missing §4'}, {'complete': True}, _outcome('satisfied')
    )
    hooks.register(
        Junction.COMPLETION_PROPOSED, _hook(await _seeded_store(_R1), verifier, output=True)
    )
    ctx = await agent.run('go')
    assert [kind_of(b) for b in ctx.current_traj].count(HistoryKind.VERDICT) == 1
    assert agent._verdict is not None and agent._verdict.status is VerdictStatus.PASS  # pyright: ignore[reportPrivateUsage]


async def test_a_failing_verifier_is_fail_open_and_unverified() -> None:
    # 验收器自身失败：放行但标 unverified、不打回（§10）——不伪装成通过。
    agent, hooks = _main(1)
    broken = ScriptedLLM([ScriptedTurn(content='not json')])
    hooks.register(Junction.COMPLETION_PROPOSED, _hook(await _seeded_store(_R1), broken))
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    verdict = agent._verdict  # pyright: ignore[reportPrivateUsage]
    assert verdict is not None and verdict.status is VerdictStatus.UNVERIFIED
    assert verdict.unverified_cause is not None and 'verifier_error' in verdict.unverified_cause


async def test_a_verifier_timeout_is_unverified() -> None:
    agent, hooks = _main(1)
    slow = _SlowLLM([ScriptedTurn(content=json.dumps(_outcome('satisfied')))])
    hooks.register(
        Junction.COMPLETION_PROPOSED, _hook(await _seeded_store(_R1), slow, timeout=0.05)
    )
    await agent.run('go')
    verdict = agent._verdict  # pyright: ignore[reportPrivateUsage]
    assert verdict is not None and verdict.unverified_cause == 'verifier_timeout'


async def test_block_retries_are_capped_and_the_final_verdict_stays_block() -> None:
    # block 上限 1：第一次打回，第二次不再打回、带着 block 完成——终态绝不是 pass。
    agent, hooks = _main(2)
    verifier = _llm(_outcome('unmet', evidence=True), _outcome('unmet', evidence=True))
    hooks.register(Junction.COMPLETION_PROPOSED, _hook(await _seeded_store(_R1), verifier, block=1))
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert [kind_of(b) for b in ctx.current_traj].count(HistoryKind.VERDICT) == 1
    assert agent._verdict is not None and agent._verdict.status is VerdictStatus.BLOCK  # pyright: ignore[reportPrivateUsage]


async def test_l3_off_gives_unverified_not_pass() -> None:
    # 只开 L2、L2 说做完了：没验结果就不能说通过 → unverified（评审 M1）。
    agent, hooks = _main(1)
    verifier = _llm({'complete': True})
    hooks.register(
        Junction.COMPLETION_PROPOSED,
        _hook(await _seeded_store(_R1), verifier, output=True, outcome=False),
    )
    await agent.run('go')
    verdict = agent._verdict  # pyright: ignore[reportPrivateUsage]
    assert verdict is not None and verdict.status is VerdictStatus.UNVERIFIED


async def test_a_cancelled_verifier_propagates_cancellation() -> None:
    # 取消不是验收器失败：不被 fail-open 吞掉（只接 Exception），照常上抛。
    agent, hooks = _main(1)
    cancelling = ScriptedLLM([ScriptedTurn(raises=asyncio.CancelledError())])
    hooks.register(Junction.COMPLETION_PROPOSED, _hook(await _seeded_store(_R1), cancelling))
    with pytest.raises(asyncio.CancelledError):
        await agent.run('go')


async def test_with_both_layers_off_there_is_no_verdict() -> None:
    agent, hooks = _main(1)
    hooks.register(
        Junction.COMPLETION_PROPOSED,
        _hook(await _seeded_store(_R1), _llm(), output=False, outcome=False),
    )
    await agent.run('go')
    assert agent._verdict is None  # pyright: ignore[reportPrivateUsage]
