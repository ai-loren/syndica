"""server 对宿主装配的要求。

server 只依赖 kernel：会话怎么装（`.syndica.yaml`、信任、Agent 环境白名单、开发模式的拦截、会话锁、
trace 录制）由组合根注入的实现决定，它与 CLI 是同一份代码。这里的签名只用内核公开类型与纯数据，
实现方不必 import server。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any, Protocol

from syndica.kernel import Budget, DeliveryResult, Redactor, Subscription

__all__ = ['AskBudget', 'SessionAssembly', 'SessionHandle', 'Terminal']

AskBudget = Callable[[str, Mapping[str, Any]], Awaitable[Budget | None]]
"""预算耗尽、按 `ask_human` 处置时问人：（agent id，预算视图的纯数据）→ 追加的额度或 None。"""


class Terminal(Protocol):
    """人的终端（一个 PTY）。`read` 交回空字节表示 shell 已经退出。"""

    async def read(self) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def resize(self, cols: int, rows: int) -> None: ...

    async def close(self) -> None: ...


class SessionHandle(Protocol):
    """一个打开着的会话。`subscription` 在根 Agent spawn 之前就已订阅，里面没有漏掉的人机请求。"""

    @property
    def sid(self) -> str: ...

    @property
    def root_id(self) -> str: ...

    @property
    def subscription(self) -> Subscription: ...

    async def deliver(self, target: str, text: str) -> DeliveryResult:
        """发一条人的消息给会话里的一个 Agent（与 chat 同一条投递路径）。"""
        ...

    async def answer(self, agent_id: str, request_id: str, text: str) -> DeliveryResult:
        """答一个人机请求：sender='human'、trust=TRUSTED、activate=False。"""
        ...

    async def abort(self, agent_id: str) -> None: ...

    async def snapshot(self) -> dict[str, Any]:
        """此刻的纯数据快照：`agents`（树）、`transcripts`（每个 Agent 的块与代号）、`budget`、
        `progress`。"""
        ...

    def grant(self, agent_id: str, budget: Budget) -> dict[str, Any]:
        """宿主追加预算，交回追加之后的预算视图。"""
        ...

    def redactor(self) -> Redactor: ...

    def trace_id(self) -> str | None:
        """这个会话正在写的 trace；没在记录时 None。"""
        ...

    def flags(self) -> list[dict[str, Any]]: ...

    async def artifact_list(self) -> list[dict[str, Any]]: ...

    async def artifact_versions(self, artifact_id: str) -> list[dict[str, Any]]: ...

    async def artifact_content(self, artifact_id: str, version: str) -> tuple[bytes, str]:
        """（原文，登记的 mime）。"""
        ...

    async def artifact_preview(self, artifact_id: str, version: str) -> str: ...

    async def artifact_diff(self, artifact_id: str, old: str, new: str) -> dict[str, Any]: ...

    async def artifact_feedback(
        self, artifact_id: str, version: str, text: str, lines: tuple[int, int] | None
    ) -> dict[str, Any] | None:
        """人对产物的反馈落成提议的 Rubric；锚点重开不了时交回 None。"""
        ...

    async def rubrics(self) -> dict[str, Any]: ...

    async def activate_rubric(self, rubric_id: str) -> None: ...

    def open_terminal(self, cols: int, rows: int) -> Terminal: ...

    async def agent_screen(self, handle: str, since: int, wait: float) -> dict[str, Any]:
        """Agent 终端从 `since` 起的输出（去掉了控制序列）：`text`、`end`、`running`、
        `exit_code`。"""
        ...

    async def close(self, failure: BaseException | None) -> None: ...


class SessionAssembly(Protocol):
    """组合根注入的会话装配。"""

    def redactor(self) -> Redactor:
        """不属于任何会话的外发内容（会话列表、doctor）用的脱敏器。"""
        ...

    async def open(
        self,
        *,
        resume: str | None,
        role: str,
        tokens: int | None,
        cost: Decimal | None,
        wall_clock_s: float | None,
        turns: int | None,
        ask_budget: AskBudget,
    ) -> SessionHandle: ...

    async def list_sessions(self) -> list[dict[str, Any]]:
        """落盘的会话（纯数据：sid、agent、status、turns、created_at、updated_at）。"""
        ...

    async def remove_session(self, sid: str) -> None: ...

    def roles(self) -> list[str]: ...

    def topologies(self) -> list[dict[str, Any]]: ...

    async def traces(self) -> list[dict[str, Any]]: ...

    async def trace(self, trace_id: str) -> dict[str, Any]:
        """一份 trace 的分析（`kernel.trace_analysis`）。"""
        ...

    def doctor(self) -> list[dict[str, Any]]:
        """逐项检查：每项 `name`、`ok`，失败的另有 `expected` / `received` / `next_action`。"""
        ...
