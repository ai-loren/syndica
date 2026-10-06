"""服务面的会话装配：与 chat 是同一份代码（`RuntimeCliHost.assemble_session`），由组合根注入
server。

配置在 server 启动时信任一次，此后所有会话用那份快照，运行期间不再读 `.syndica.yaml`：否则一个会话里
的 Agent 改了它，下一个会话就会在 server 进程（宿主环境、全部 API key）里导入它写的代码。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any

from syndica.cli.config import CliConfig
from syndica.cli.handoff import session_root
from syndica.cli.host import RuntimeCliHost, SessionParts, answer_request
from syndica.kernel import (
    Budget,
    BudgetView,
    DeliveryResult,
    FileSessionStore,
    Redactor,
    Runtime,
    Subscription,
)
from syndica.kernel.transcript import block_payload, budget_payload

__all__ = ['ServerAssembly', 'ServerSession']

AskBudget = Callable[[str, Mapping[str, Any]], Awaitable[Budget | None]]


class ServerSession:
    def __init__(self, host: RuntimeCliHost, parts: SessionParts) -> None:
        self._host = host
        self._parts = parts

    @property
    def sid(self) -> str:
        return self._parts.agent_id

    @property
    def root_id(self) -> str:
        return self._parts.agent_id

    @property
    def subscription(self) -> Subscription:
        return self._parts.subscription

    @property
    def runtime(self) -> Runtime:
        return self._parts.runtime

    async def deliver(self, target: str, text: str) -> DeliveryResult:
        return await self.runtime.send(target, text)

    async def answer(self, agent_id: str, request_id: str, text: str) -> DeliveryResult:
        return await answer_request(self.runtime, agent_id, request_id, text)

    async def abort(self, agent_id: str) -> None:
        await self.runtime.abort(agent_id)

    async def snapshot(self) -> dict[str, Any]:
        runtime = self.runtime
        agents: list[dict[str, Any]] = []
        transcripts: dict[str, dict[str, Any]] = {}
        for info in await runtime.registry.list():
            agents.append(
                {
                    'agent_id': info.agent_id,
                    'name': info.name,
                    'parent_id': info.parent_id,
                    'status': info.status.value,
                }
            )
            agent = runtime._table.get(info.agent_id)  # pyright: ignore[reportPrivateUsage]
            if agent is not None:
                context = agent.context
                transcripts[info.agent_id] = {
                    'generation': context._generation,  # pyright: ignore[reportPrivateUsage]
                    'blocks': [block_payload(block) for block in context.current_traj],
                }
        view = runtime._budget_of(self.root_id)  # pyright: ignore[reportPrivateUsage]
        return {
            'agents': agents,
            'transcripts': transcripts,
            'budget': None if view is None else budget_payload(view),
            'progress': None,
        }

    def grant(self, agent_id: str, budget: Budget) -> dict[str, Any]:
        runtime = self.runtime
        runtime._grant_budget(agent_id, budget)  # pyright: ignore[reportPrivateUsage]
        view = runtime._budget_of(agent_id)  # pyright: ignore[reportPrivateUsage]
        return {} if view is None else budget_payload(view)

    def redactor(self) -> Redactor:
        return self._parts.redactor

    async def close(self, failure: BaseException | None) -> None:
        await self._host.close_session(self._parts, failure)


class ServerAssembly:
    def __init__(self, host: RuntimeCliHost, *, path: str, config: CliConfig) -> None:
        self._host = host
        self._path = path
        self._config = config
        self._digest = host.config_digest(path)
        self._redactor = host.redactor_for(config)

    def redactor(self) -> Redactor:
        return self._redactor

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
    ) -> ServerSession:
        async def ask_human(agent_id: str, view: BudgetView) -> Budget | None:
            return await ask_budget(agent_id, budget_payload(view))

        parts = await self._host.assemble_session(
            self._path,
            max_turns=turns,
            trace=None,
            argv=('syndica', 'up'),
            trusted_output=(),
            resume=resume,
            on_exhausted='ask_human',
            tokens=tokens,
            cost=cost,
            wall_clock_s=wall_clock_s,
            ask_human=ask_human,
            role=role,
            config=self._config,
        )
        return ServerSession(self._host, parts)

    async def list_sessions(self) -> list[dict[str, Any]]:
        store = FileSessionStore(session_root(self._host.workspace_root))
        return [
            {
                'sid': meta.sid,
                'agent': meta.agent,
                'status': meta.status.value,
                'turns': meta.turns,
                'created_at': meta.created_at,
                'updated_at': meta.updated_at,
            }
            for meta in await store.list_metas()
        ]

    async def remove_session(self, sid: str) -> None:
        await self._host.remove_session(sid)

    def roles(self) -> list[str]:
        return list(self._config.catalog.names())

    def doctor(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = [{'name': 'config', 'ok': True}]
        if self._host.config_digest(self._path) != self._digest:
            checks[0] = {
                'name': 'config',
                'ok': False,
                'expected': f'{self._path} 与 server 启动时信任的那份一致',
                'received': '文件在 server 启动之后改过；server 仍用启动时的那份',
                'next_action': (
                    '看一遍改动，用 syndica config trust 信任新的摘要，'
                    '再 syndica down 与 syndica up 重启 server'
                ),
            }
        checks.extend(self._host.model_checks(self._config))
        return checks
