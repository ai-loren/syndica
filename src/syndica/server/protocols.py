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

__all__ = ['AskBudget', 'SessionAssembly', 'SessionHandle']

AskBudget = Callable[[str, Mapping[str, Any]], Awaitable[Budget | None]]
"""预算耗尽、按 `ask_human` 处置时问人：（agent id，预算视图的纯数据）→ 追加的额度或 None。"""


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

    def doctor(self) -> list[dict[str, Any]]:
        """逐项检查：每项 `name`、`ok`，失败的另有 `expected` / `received` / `next_action`。"""
        ...
