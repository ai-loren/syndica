"""RubricStore：Goal 与 Rubric 的存储契约（spec §7.6 / §8.2）。

Goal / Rubric **独立于对话存储**（§8.2：Goal 不随上下文压缩丢失），按 `scope`（会话 id）分隔——一个
Runtime 的两个任务、或同一 store 的两个会话，各有各的 Goal 与 Rubric（否则正中「作用在错主体」）。

写入面只收 `rubric_redaction` 铸造的类型（已逐叶脱敏），第三方 store 绕不过脱敏。**先私有**（不进
`kernel.__all__`）：`SessionStore` 契约已发布、Rubric 契约未发布，第三方验收后端暂无公开契约可自证；
v1.0 冻结公开 API 时再定（同 `MemoryStore`）。

`put_rubrics` 是**upsert**（按 id 增 / 改，不删别的）：Goal 变更重新投影时同 id 更新、人确认过的反馈
Rubric（别的 id）照样存活（§7.7 / 评审 H10）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from syndica.kernel.acceptance import Goal, Rubric
from syndica.kernel.rubric_redaction import RedactedGoal, RedactedRubric

__all__ = ['RubricStore']


@runtime_checkable
class RubricStore(Protocol):
    async def put_goal(self, scope: str, goal: RedactedGoal) -> None:
        """设这个 scope 的 Goal（唯一权威）。只收脱敏类型。"""
        ...

    async def get_goal(self, scope: str) -> Goal | None:
        """这个 scope 的 Goal；没设过是 None。返回快照，改它不影响存储。"""
        ...

    async def put_rubrics(self, scope: str, rubrics: list[RedactedRubric]) -> None:
        """upsert 一批 Rubric（按 id 增 / 改，不删别的）：重新投影更新同 id、反馈 Rubric 存活。"""
        ...

    async def rubrics(self, scope: str, *, include_proposed: bool = False) -> list[Rubric]:
        """这个 scope 的 Rubric，按登记顺序。默认只给 `active` 的；`include_proposed` 把
        `proposed`（待人确认的反馈）也带上（§7.7）。"""
        ...

    async def propose(self, scope: str, rubric: RedactedRubric) -> None:
        """提议一条 `proposed` Rubric（§7.7 人反馈）：进库但默认不进验收，等 `activate`。"""
        ...

    async def activate(self, scope: str, rubric_id: str) -> None:
        """把一条 `proposed` Rubric 置为 `active`（人确认后生效，§7.7）。认不出 id 不报、无操作。"""
        ...
