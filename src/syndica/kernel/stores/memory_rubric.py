"""RubricStore 的内存实现（spec §7.6 / §8.2）。

按 scope 存 Goal 与 Rubric（id → Rubric，登记顺序）。`put_rubrics` 替换**投影出的**那批：不在新集合
里的 projected 条删掉（旧 Goal 的要求不能拿去验新 Goal，评审 H7），人的反馈条（`FEEDBACK`）不碰——同
id 撞上时反馈条胜出。`propose` 恒存成 `proposed` / `feedback`、不覆盖已有的 id；`activate` 只把
proposed 翻成 active（§7.7）。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from syndica.kernel.acceptance import Goal, Rubric, RubricOrigin, RubricStatus
from syndica.kernel.rubric_redaction import RedactedGoal, RedactedRubric

__all__ = ['InMemoryRubricStore']


class InMemoryRubricStore:
    def __init__(self) -> None:
        self._goals: dict[str, Goal] = {}
        self._rubrics: dict[str, dict[str, Rubric]] = {}
        self._lock = asyncio.Lock()

    async def put_goal(self, scope: str, goal: RedactedGoal) -> None:
        async with self._lock:
            self._goals[scope] = goal.goal

    async def get_goal(self, scope: str) -> Goal | None:
        return self._goals.get(scope)

    async def put_rubrics(self, scope: str, rubrics: list[RedactedRubric]) -> None:
        async with self._lock:
            table = self._rubrics.setdefault(scope, {})
            replace_projected(table, [r.rubric for r in rubrics])

    async def rubrics(self, scope: str, *, include_proposed: bool = False) -> list[Rubric]:
        table = self._rubrics.get(scope, {})
        return [
            rubric
            for rubric in table.values()
            if include_proposed or rubric.status is RubricStatus.ACTIVE
        ]

    async def propose(self, scope: str, rubric: RedactedRubric) -> None:
        async with self._lock:
            table = self._rubrics.setdefault(scope, {})
            add_proposal(table, rubric.rubric)

    async def activate(self, scope: str, rubric_id: str) -> None:
        async with self._lock:
            table = self._rubrics.get(scope, {})
            existing = table.get(rubric_id)
            if existing is not None and existing.status is RubricStatus.PROPOSED:
                table[rubric_id] = replace(existing, status=RubricStatus.ACTIVE)


def replace_projected(table: dict[str, Rubric], incoming: list[Rubric]) -> None:
    """就地把 `table` 里投影出的那批换成 `incoming`（两个实现共用，语义只写一处）。"""
    wanted = {r.id: replace(r, origin=RubricOrigin.PROJECTED) for r in incoming}
    stale = [
        rid for rid, r in table.items() if r.origin is RubricOrigin.PROJECTED and rid not in wanted
    ]
    for rid in stale:
        del table[rid]
    for rid, rubric in wanted.items():
        existing = table.get(rid)
        if existing is not None and existing.origin is RubricOrigin.FEEDBACK:
            continue  # 人的反馈条不被投影覆盖（评审 H8）
        table[rid] = rubric


def add_proposal(table: dict[str, Rubric], rubric: Rubric) -> None:
    """就地加一条反馈提议：恒 `proposed` / `feedback`；id 已在就不动，不降级已生效的条。"""
    if rubric.id in table:
        return
    table[rubric.id] = replace(rubric, status=RubricStatus.PROPOSED, origin=RubricOrigin.FEEDBACK)
