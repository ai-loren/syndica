"""RubricStore 的内存实现（spec §7.6 / §8.2）。

按 scope 存 Goal 与 Rubric（id → Rubric，登记顺序）。`put_rubrics` 是 upsert：重新投影更新同 id、
反馈 Rubric 存活。`propose` 存成 `proposed`、`activate` 翻成 `active`（§7.7）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from syndica.kernel.acceptance import Goal, Rubric, RubricStatus
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
            for redacted in rubrics:
                table[redacted.rubric.id] = redacted.rubric

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
            table[rubric.rubric.id] = replace(rubric.rubric, status=RubricStatus.PROPOSED)

    async def activate(self, scope: str, rubric_id: str) -> None:
        async with self._lock:
            table = self._rubrics.get(scope, {})
            existing = table.get(rubric_id)
            if existing is not None:
                table[rubric_id] = replace(existing, status=RubricStatus.ACTIVE)
