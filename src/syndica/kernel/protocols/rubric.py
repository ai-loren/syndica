"""RubricStore：Goal 与 Rubric 的存储契约（spec §7.6 / §8.2）。

Goal / Rubric **独立于对话存储**（§8.2：Goal 不随上下文压缩丢失），按 `scope`（会话 id）分隔——一个
Runtime 的两个任务、或同一 store 的两个会话，各有各的 Goal 与 Rubric（否则正中「作用在错主体」）。

写入面只收 `rubric_redaction` 铸造的类型（已逐叶脱敏），第三方 store 绕不过脱敏。**先私有**（不进
`kernel.__all__`）：`SessionStore` 契约已发布、Rubric 契约未发布，第三方验收后端暂无公开契约可自证；
v1.0 冻结公开 API 时再定（同 `MemoryStore`）。

`put_rubrics` 替换**投影出的**那批（`origin=PROJECTED`）：不在新集合里的 projected 条删掉——旧 Goal
的要求不能拿去验新 Goal（评审 H7）；人的反馈条（`FEEDBACK`）不碰，同 id 撞上时反馈条胜出（评审 H8）
。`propose` 恒存成 `proposed` / `feedback`，不覆盖已有 id；`activate` 只把 proposed 翻成 active。"""

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
        """把投影出的那批换成 `rubrics`：删掉不在其中的 projected 条、不碰反馈条（空列表 = 退役全部
        projected）。只收脱敏类型。"""
        ...

    async def rubrics(self, scope: str, *, include_proposed: bool = False) -> list[Rubric]:
        """这个 scope 的 Rubric，按登记顺序。默认只给 `active` 的；`include_proposed` 把
        `proposed`（待人确认的反馈）也带上（§7.7）。"""
        ...

    async def propose(self, scope: str, rubric: RedactedRubric) -> None:
        """提议一条反馈 Rubric（§7.7）：不论传进来的 status / origin，恒存成 `proposed` /
        `feedback`；id 已在则不动。进库但默认不进验收，等 `activate`。"""
        ...

    async def activate(self, scope: str, rubric_id: str) -> None:
        """把一条 `proposed` Rubric 置为 `active`（人确认后生效，§7.7）。认不出 id 或不是 proposed
        时无操作。"""
        ...
