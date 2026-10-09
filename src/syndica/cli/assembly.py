"""服务面的会话装配：与 chat 是同一份代码（`RuntimeCliHost.assemble_session`），由组合根注入
server。

配置在 server 启动时信任一次，此后所有会话用那份快照，运行期间不再读 `.syndica.yaml`：否则一个会话里
的 Agent 改了它，下一个会话就会在 server 进程（宿主环境、全部 API key）里导入它写的代码。
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import fcntl
import hashlib
import os
import pty
import signal
import struct
import subprocess
import termios
import uuid
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from syndica.cli.config import CliConfig
from syndica.cli.environment import AgentEnvironment
from syndica.cli.handoff import artifact_root, rubric_root, session_root, trace_root
from syndica.cli.host import RuntimeCliHost, SessionParts, answer_request
from syndica.kernel import (
    ArtifactRef,
    Budget,
    BudgetView,
    ConfigError,
    DeliveryResult,
    FileSessionStore,
    FileTaint,
    FileTraceStore,
    Redactor,
    Runtime,
    Subscription,
    Tool,
    WorkspaceRouter,
)
from syndica.kernel.acceptance_inject import meaningful_quote
from syndica.kernel.feedback import propose_feedback_rubric
from syndica.kernel.stores.file_artifact import FileArtifactStore
from syndica.kernel.stores.file_rubric import FileRubricStore
from syndica.kernel.trace_analysis import analyze_trace
from syndica.kernel.transcript import artifact_payload, block_payload, budget_payload

__all__ = ['ServerAssembly', 'ServerSession']

AskBudget = Callable[[str, Mapping[str, Any]], Awaitable[Budget | None]]


class ServerSession:
    def __init__(self, host: RuntimeCliHost, parts: SessionParts) -> None:
        self._host = host
        self._parts = parts
        self._terminals: set[HumanTerminal] = set()

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

    def install_tools(self, tools: Mapping[str, Tool]) -> None:
        agent = self.runtime._table.get(self.root_id)  # pyright: ignore[reportPrivateUsage]
        if agent is None:
            raise ConfigError(
                expected='根 Agent 已经在表里',
                received='找不到它',
                next_action='这是 server 的内部错误，重开一次请求',
            )
        clash = sorted(set(tools) & set(agent.options.tools))
        if clash:
            raise ConfigError(
                expected='客户端工具名不与 Syndica 工具重名',
                received=f'撞上 {clash!r}',
                next_action='给客户端工具换一个名字，例如 client_lookup',
            )
        agent.install_runtime_tools(dict(tools))

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

    def trace_id(self) -> str | None:
        recorder = self._parts.recording.recorder
        return None if recorder is None else recorder.trace_id

    def flags(self) -> list[dict[str, Any]]:
        values = self.runtime.flags
        return [
            {
                'key': declaration.key,
                'value': values.get(declaration.key, declaration.default),
                'default': declaration.default,
                'description': declaration.description,
                'choices': list(declaration.choices),
            }
            for declaration in self.runtime.flag_declarations
        ]

    async def artifact_list(self) -> list[dict[str, Any]]:
        store = _artifact_store(self._host.workspace_root)
        if store is None:
            return []
        return [
            {**artifact_payload(ref), 'versions': len(await store.versions_of(ref.id))}
            for ref in await store.list()
        ]

    async def artifact_versions(self, artifact_id: str) -> list[dict[str, Any]]:
        return [artifact_payload(ref) for ref in await self._versions_of(artifact_id)]

    async def artifact_content(self, artifact_id: str, version: str) -> tuple[bytes, str]:
        ref = await self._ref(artifact_id, version)
        return await _Reader(self._parts.workspace).read_bytes(ref), ref.mime

    async def artifact_preview(self, artifact_id: str, version: str) -> str:
        ref = await self._ref(artifact_id, version)
        preview = await self._host.artifact_handlers.for_(ref).preview(
            ref, _Reader(self._parts.workspace)
        )
        return preview.text

    async def artifact_diff(self, artifact_id: str, old: str, new: str) -> dict[str, Any]:
        before = await self._ref(artifact_id, old)
        after = await self._ref(artifact_id, new)
        reader = _Reader(self._parts.workspace)
        diff = await self._host.artifact_handlers.for_(after).semantic_diff(before, after, reader)
        lines = difflib.unified_diff(
            (await reader.read(before)).splitlines(),
            (await reader.read(after)).splitlines(),
            fromfile=f'{before.path}@{old}',
            tofile=f'{after.path}@{new}',
            lineterm='',
        )
        return {'summary': diff.summary, 'changed': diff.changed, 'unified': '\n'.join(lines)}

    async def artifact_feedback(
        self, artifact_id: str, version: str, text: str, lines: tuple[int, int] | None
    ) -> dict[str, Any] | None:
        """人对一个产物（可带行范围）的反馈落成一条提议的 Rubric，仍要人激活才生效（§7.7）。"""
        ref = await self._ref(artifact_id, version)
        if not meaningful_quote(text):
            raise ConfigError(
                expected='反馈至少有 4 个非空白字符：它就是这条 Rubric 的锚点',
                received=f'收到 {len(text.strip())} 个字符',
                next_action='把反馈写具体一点再提交',
            )
        quotes: dict[str, str] = {}
        if lines is not None:
            content = (await _Reader(self._parts.workspace).read(ref)).splitlines()
            start, end = lines
            quote = '\n'.join(content[start - 1 : end])
            # 选中的几行太短就只锚反馈本身：短引文在任何来源里都「找得到」，等于没有锚点。
            if meaningful_quote(quote):
                quotes[f'{ref.path}#L{start}-L{end}'] = quote
        rubric_id = f'feedback-{uuid.uuid4().hex[:12]}'
        rubric = await propose_feedback_rubric(
            scope=self.sid,
            rubric_id=rubric_id,
            feedback=text,
            locator=f'msg:{rubric_id}',
            store=FileRubricStore(rubric_root(self._host.workspace_root)),
            redactor=self._parts.redactor,
            artifacts=quotes,
        )
        return None if rubric is None else _rubric(rubric)

    async def rubrics(self) -> dict[str, Any]:
        store = FileRubricStore(rubric_root(self._host.workspace_root))
        goal = await store.get_goal(self.sid)
        rubrics = await store.rubrics(self.sid, include_proposed=True)
        return {
            'goal': None if goal is None else goal.intent,
            'rubrics': [_rubric(rubric) for rubric in sorted(rubrics, key=lambda r: r.id)],
        }

    async def activate_rubric(self, rubric_id: str) -> None:
        store = FileRubricStore(rubric_root(self._host.workspace_root))
        current = {r.id: r for r in await store.rubrics(self.sid, include_proposed=True)}
        target = current.get(rubric_id)
        if target is None or target.status.value != 'proposed':
            raise ConfigError(
                expected=f'会话 {self.sid} 里一条提议中的 Rubric',
                received=f'{rubric_id!r}：{"没有这一条" if target is None else "这一条已经生效"}',
                next_action='重取这个会话的 Rubric 列表，挑一条 proposed 的',
            )
        await store.activate(self.sid, rubric_id)

    def open_terminal(self, cols: int, rows: int) -> HumanTerminal:
        terminal = HumanTerminal(self._parts.environment, self._host.workspace_root, cols, rows)
        self._terminals.add(terminal)
        return terminal

    async def agent_screen(
        self, agent_id: str, handle: str, since: int, wait: float
    ) -> dict[str, Any]:
        capture = await self._parts.workspace.pty_capture_of(
            agent_id, handle, since=since, wait=wait
        )
        return {
            'text': capture.text,
            'end': capture.end,
            'running': capture.running,
            'exit_code': capture.exit_code,
            'truncated': capture.truncated,
        }

    async def close(self, failure: BaseException | None) -> None:
        for terminal in list(self._terminals):
            await terminal.close()
        await self._host.close_session(self._parts, failure)

    async def _versions_of(self, artifact_id: str) -> list[ArtifactRef]:
        store = _artifact_store(self._host.workspace_root)
        versions = [] if store is None else await store.versions_of(artifact_id)
        if not versions:
            raise ConfigError(
                expected=f'产物索引里有 {artifact_id!r}',
                received='没有：这个工作区没登记过它（只有装了 acceptance 的会话才登记产物）',
                next_action='重取产物列表，按列出的 id 再请求',
            )
        return versions

    async def _ref(self, artifact_id: str, version: str) -> ArtifactRef:
        for ref in await self._versions_of(artifact_id):
            if ref.version == version:
                return ref
        raise ConfigError(
            expected=f'{artifact_id!r} 有版本 {version!r}',
            received='索引里没有这一版',
            next_action='重取这个产物的版本列表',
        )


class ServerAssembly:
    def __init__(self, host: RuntimeCliHost, *, path: str, config: CliConfig) -> None:
        self._host = host
        self._path = path
        self._config = config
        self._digest = host.config_digest(path)
        self._redactor = host.redactor_for(config)
        # 进程里所有会话共用一份，基线在 server 启动时取：每个会话各取一份基线，A 会话从不可信
        # 内容写下的文件就进了之后才打开的 B 会话的基线，B 读到它时是可信的（硬约束 14）。
        self._taint = FileTaint(host.workspace_root)

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
            taint=self._taint,
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

    def topologies(self) -> list[dict[str, Any]]:
        return [
            {
                'name': name,
                'slots': [
                    {'name': slot.name, 'agent': slot.agent, 'is_root': slot.is_root}
                    for slot in topology.slots
                ],
            }
            for name, topology in self._config.topologies.items()
        ]

    async def traces(self) -> list[dict[str, Any]]:
        store = _trace_store(self._host.workspace_root)
        if store is None:
            return []
        return [
            {
                'trace_id': header.trace_id,
                'started_ns': header.started_ns,
                'ended_ns': header.ended_ns,
                'status': None if header.status is None else header.status.value,
            }
            for header in sorted(await store.traces(), key=lambda h: h.started_ns, reverse=True)
        ]

    async def trace(self, trace_id: str) -> dict[str, Any]:
        store = _trace_store(self._host.workspace_root)
        header = None if store is None else await store.get(trace_id)
        if store is None or header is None:
            raise ConfigError(
                expected=f'.syndica/traces 里有 trace {trace_id!r}',
                received='没有',
                next_action='重取 trace 列表，按列出的 id 再请求',
            )
        return analyze_trace(header, await store.spans(trace_id), await store.edges(trace_id))

    def doctor(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = [{'name': 'config', 'ok': True}]
        try:
            digest = self._host.config_digest(self._path)
        except ConfigError as error:
            # 改坏了的配置不影响在跑的 server（它用启动时的快照）：如实报这一项，其余照常检查。
            checks[0] = {
                'name': 'config',
                'ok': False,
                'expected': f'{self._path} 是一份能读的配置，且与 server 启动时信任的那份一致',
                'received': f'文件在 server 启动之后改过，现在读不出来：{error.received}',
                'next_action': (
                    f'{error.next_action}；改好之后用 syndica config trust 信任新的摘要，'
                    '再 syndica down 与 syndica up 重启 server'
                ),
            }
            digest = self._digest
        if digest != self._digest:
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


def _artifact_store(root: Path) -> FileArtifactStore | None:
    location = artifact_root(root)
    return FileArtifactStore(location) if location.is_dir() else None


def _trace_store(root: Path) -> FileTraceStore | None:
    location = trace_root(root)
    return FileTraceStore(location) if location.is_dir() else None


def _rubric(rubric: Any) -> dict[str, Any]:
    return {
        'id': rubric.id,
        'statement': rubric.statement,
        'kind': rubric.kind.value,
        'status': rubric.status.value,
        'origin': rubric.origin.value,
    }


class _Reader:
    """按 `ref.version` 读产物：此刻的文件还是那一版就直接读，否则从版本层取那一版的原文。"""

    def __init__(self, router: WorkspaceRouter) -> None:
        self._router = router

    async def read_bytes(self, ref: ArtifactRef) -> bytes:
        with contextlib.suppress(Exception):
            data = await self._router.portal().read_bytes(ref.path)
            if f'sha256:{hashlib.sha256(data).hexdigest()}' == ref.version:
                return data
        versions = self._router.versions
        if versions is None:
            raise ConfigError(
                expected=f'{ref.path} 的版本 {ref.version} 取得回来',
                received='文件已经不是这一版，而工作区没开版本层',
                next_action='在 .syndica.yaml 里打开 workspace.versions 之后，新写的版本才取得回来',
            )
        return versions.content(ref.path, ref.version)

    async def read(self, ref: ArtifactRef) -> str:
        return (await self.read_bytes(ref)).decode('utf-8', errors='replace')


class HumanTerminal:
    """人的终端：跑在 Agent 的环境里（白名单变量、临时的 git / gh 配置，没有宿主的密钥）。**不是
    沙箱**：它能读写人能读写的一切，与在工作区里开一个 shell 相同。用 `/bin/sh -i` 而不是人的登录
    shell：登录 shell 会读 rc 文件，rc 里 export 的密钥就又回到这个环境里了。"""

    def __init__(self, environment: AgentEnvironment, cwd: Path, cols: int, rows: int) -> None:
        master, slave = pty.openpty()
        self._master = master
        _set_size(master, cols, rows)
        self._process = subprocess.Popen(
            ['/bin/sh', '-i'],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=dict(environment.values),
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self._output: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master, self._readable)
        self._closed = False

    def _readable(self) -> None:
        try:
            chunk = os.read(self._master, 65536)
        except OSError:
            chunk = b''
        if not chunk:
            self._loop.remove_reader(self._master)
        self._output.put_nowait(chunk)

    async def read(self) -> bytes:
        """下一段输出；空字节表示 shell 已经退出。"""
        return await self._output.get()

    def write(self, data: bytes) -> None:
        os.write(self._master, data)

    def resize(self, cols: int, rows: int) -> None:
        _set_size(self._master, cols, rows)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.remove_reader(self._master)
        # 交互 shell 开着作业控制，前台作业在它自己的进程组里：只杀 shell 的组，一个忽略了 SIGHUP 的
        # 前台命令会在关掉终端之后继续跑。后台作业与关掉一个终端窗口时一样，不在此列。
        groups = {self._process.pid}
        with contextlib.suppress(OSError):
            groups.add(os.tcgetpgrp(self._master))
        for group in groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
        os.close(self._master)
        await asyncio.to_thread(self._process.wait)


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
