"""开发期 Module（spec D4、D11）：拦下 git 写命令与删根，并记下每个 Agent 做了什么。

只靠处置表挡不住：读过命令输出之后，高风险调用按 `untrusted_high_risk` 处置、不再问宿主策略，`ask`
时会问人、`allow` 时会放行。所以这两族在更早的切点上拦，与那个 Flag 取什么值无关。

拦截交回的 `ToolResult` 标 `TRUSTED`：这段文字出自宿主，不是命令输出。标成不可信会把整条轨迹变成
不可信，此后每一次越界确认都直接变成拒绝。

记录挂在服务 `runtime.dev_log` 上（每个 Runtime 一份）：Agent 的登记、Write / Edit / Bash 调用、
每次运行的结局与最后一段文字，以及 Reviewer 每次运行开始与结束时的工作树指纹。宿主据此判断 Reviewer
的结论能不能用（D14），取不到的指纹记成缺失而不是当成「没有改动」。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from syndica import __version__
from syndica.cli.checks import CheckResult, parse_check
from syndica.cli.host_git import Change, HostGit
from syndica.cli.policy import dev_guard_judge
from syndica.kernel import (
    BaseAgent,
    ConfigError,
    Context,
    DangerJudge,
    DangerVerdict,
    HookResult,
    HooksCenter,
    Junction,
    RiskLevel,
    Runtime,
    RuntimeModule,
    Tool,
    ToolResult,
    TrustLevel,
)

__all__ = [
    'AgentEntry',
    'AgentRun',
    'DevLog',
    'DevLogSnapshot',
    'DevModule',
    'Fingerprint',
    'ToolEntry',
    'last_answer',
]

logger = logging.getLogger(__name__)

_LEADS = {
    'shell.git_state_change': '这条命令会改变 git 状态（提交、推送、改引用或配置、写 .git/）。',
    'shell.workspace_root_removal': '这条命令会删掉整个工作区根目录，或根下的全部内容。',
}
_ALTERNATIVES = (
    '换个做法继续：\n'
    '- 看 git 状态用只读子命令：git status、git diff、git log、git show；\n'
    '- 列文件、搜内容时要排除 .git，用 Glob / Grep 工具或 git ls-files，命令里不要写 .git 路径；\n'
    '- 清理生成物时删具体的目录（如 rm -r build），不要删 .、* 或上级目录；\n'
    '- 改完就在最终回答里写清改了什么，提交与推送由宿主在复核之后执行。'
)
_WRITERS = frozenset({'Write', 'Edit'})


def _explained(verdict: DangerVerdict) -> str:
    lead = _LEADS.get(verdict.rule_id, verdict.reason)
    return (
        f'已拦截（{verdict.rule_id}）：{lead}开发期这件事由宿主做，Agent 不能自己做，也不必问人。\n'
        f'{_ALTERNATIVES}'
    )


class _Guard:
    def __init__(self, judge: DangerJudge) -> None:
        self._judge = judge

    @property
    def name(self) -> str:
        return 'dev-guard'

    def register(self, hooks: HooksCenter) -> None:
        hooks.register(Junction.PRE_TOOL_USE, self._guard)

    async def _guard(self, **kwargs: Any) -> HookResult:
        tool = cast(Tool, kwargs['tool'])
        # 与处置闸同一套约定：`()` 声明没有会被执行的参数；没声明（None）按每个参数都会执行来判。
        # 跳过没声明的工具，`untrusted_high_risk=ask` 下它的 git 写命令就从拦下降成问人。
        if tool.__tool_command_args__ == ():
            return HookResult()
        verdict = self._judge.judge(tool, cast(Mapping[str, Any], kwargs['args']))
        if verdict.level is RiskLevel.NONE:
            return HookResult()
        return HookResult(
            skip=True, result=ToolResult(output=_explained(verdict), trust=TrustLevel.TRUSTED)
        )


@dataclass(frozen=True)
class AgentEntry:
    """插件工厂登记的 Agent：序号早于它的任何工具调用。`parent_id` 在它第一次开始运行时补上。"""

    seq: int
    agent_id: str
    role: str
    parent_id: str | None


@dataclass(frozen=True)
class ToolEntry:
    """一次成功的 Write / Edit、一次 Bash 调用，或一个后台命令第一次被看到已经结束（`BashOutput`，
    记的是启动它的那条命令）。`target` 是命令或路径；`handle` 只在 Bash 启动后台命令时有。"""

    seq: int
    agent_id: str
    role: str
    tool: str
    target: str | None
    exit_code: int | None
    check: CheckResult | None
    handle: str | None = None


@dataclass(frozen=True)
class Fingerprint:
    """工作树指纹：改动与每个文件的 blob id。`changes` 是 None 表示没取到，`error` 说明原因。"""

    changes: tuple[Change, ...] | None
    error: str | None = None


@dataclass(frozen=True)
class AgentRun:
    """一个 Agent 的一次运行（一次 `run`）。`stop_reason` 是 None：还在跑，或被取消。`start` 与
    `end` 只有 Reviewer 才取；以错误结束的运行没有 `end`。`started` / `ended` 是开始与结束时的全局
    序号：几个 Reviewer 并发（同一条消息里派两个、分叉出的子评审）时，「最后一个」按结束的先后算，
    开始的先后说明不了谁的结论在后。"""

    agent_id: str
    role: str
    stop_reason: str | None
    final_text: str | None
    start: Fingerprint | None
    end: Fingerprint | None
    started: int = 0
    ended: int | None = None


@dataclass(frozen=True)
class DevLogSnapshot:
    agents: tuple[AgentEntry, ...]
    tools: tuple[ToolEntry, ...]
    runs: tuple[AgentRun, ...]


class DevLog:
    """一个 Runtime 的开发期记录。只在事件循环上改，每个方法里没有挂起点。"""

    def __init__(self) -> None:
        self._next = 1
        self._agents: dict[str, AgentEntry] = {}
        self._tools: list[ToolEntry] = []
        self._runs: list[AgentRun] = []
        self._commands: dict[str, str] = {}
        self._finished: set[str] = set()

    def _sequence(self) -> int:
        seq = self._next
        self._next += 1
        return seq

    def enroll(self, agent_id: str, role: str) -> int:
        entry = AgentEntry(self._sequence(), agent_id, role, None)
        self._agents[agent_id] = entry
        return entry.seq

    def set_parent(self, agent_id: str, parent_id: str | None) -> None:
        self._agents[agent_id] = replace(self._agents[agent_id], parent_id=parent_id)

    def record(
        self,
        agent_id: str,
        role: str,
        tool: str,
        target: str | None,
        exit_code: int | None = None,
        check: CheckResult | None = None,
        handle: str | None = None,
    ) -> None:
        self._tools.append(
            ToolEntry(self._sequence(), agent_id, role, tool, target, exit_code, check, handle)
        )

    def started(self, handle: str, command: str) -> None:
        self._commands[handle] = command

    def first_finish(self, handle: str) -> str | None:
        """这个后台命令第一次被看到已经结束时交回启动它的命令，以后交回 None。没见过这个 handle
        时交回空串：仍然只记一次，只是不知道命令。"""
        if handle in self._finished:
            return None
        self._finished.add(handle)
        return self._commands.get(handle, '')

    def begin(self, agent_id: str, role: str, start: Fingerprint | None) -> int:
        self._runs.append(AgentRun(agent_id, role, None, None, start, None, self._sequence()))
        return len(self._runs) - 1

    def finish(
        self,
        index: int,
        stop_reason: str,
        final_text: str | None,
        end: Fingerprint | None,
    ) -> None:
        self._runs[index] = replace(
            self._runs[index],
            stop_reason=stop_reason,
            final_text=final_text,
            end=end,
            ended=self._sequence(),
        )

    def snapshot(self) -> DevLogSnapshot:
        return DevLogSnapshot(tuple(self._agents.values()), tuple(self._tools), tuple(self._runs))


def _check_meta(check: CheckResult) -> dict[str, object]:
    return {
        'schema': 1,
        'tool': check.tool,
        'status': check.status,
        'counts': dict(check.counts),
        'summary': check.summary,
    }


def last_answer(context: Context, start: int) -> str | None:
    """这次运行（轨迹 `start` 之后）最后一个 assistant 块里的文字；它没有文字（只调了工具）就是
    None。不再往前找：往前找到的是模型已经被后来的话取代的一段，比如调工具之前随口说的
    `verdict: pass`。内容是分段列表时（带 thinking 的后端）拼起它的文本段。"""
    for block in reversed(context.current_traj[start:]):
        if block.role != 'assistant':
            continue
        content: object = block.content
        if isinstance(content, str):
            return content or None
        if isinstance(content, list):
            parts = [
                str(cast(Mapping[str, object], part).get('text', ''))
                for part in cast(list[object], content)
                if isinstance(part, Mapping)
                and cast(Mapping[str, object], part).get('type') == 'text'
            ]
            return ''.join(parts) or None
        return None
    return None


class _Recorder:
    def __init__(
        self,
        log: DevLog,
        runtime: Runtime,
        agent_id: str,
        role: str,
        git: HostGit | None,
    ) -> None:
        self._log = log
        self._runtime = runtime
        self._agent_id = agent_id
        self._role = role
        self._git = git
        self._parent_known = False
        self._run: int | None = None
        self._start = 0

    @property
    def name(self) -> str:
        return 'dev-log'

    def register(self, hooks: HooksCenter) -> None:
        hooks.register(Junction.POST_TOOL_USE, self._recorded)
        hooks.register(Junction.AGENT_START, self._began)
        hooks.register(Junction.AGENT_END, self._ended)
        hooks.register(Junction.AGENT_ERROR, self._failed)

    async def _recorded(self, **kwargs: Any) -> HookResult:
        name = cast(Tool, kwargs['tool']).__tool_name__
        args = cast(Mapping[str, Any], kwargs['args'])
        result = cast(ToolResult, kwargs['result'])
        if name in _WRITERS:
            # 只记写成了的：没写成的 Edit（原文没找到）记进来，之后 Bash 改了同一个文件，改动就会
            # 被当成 Write / Edit 产生的。写成了的结果在 meta 里带着路径。
            if 'path' in result.meta:
                self._log.record(self._agent_id, self._role, name, _text(args.get('path')))
            return HookResult()
        if name == 'Bash':
            command = _text(args.get('command'))
            if 'exit_code' not in result.meta:
                handle = _text(result.meta.get('handle'))
                if handle is not None and command is not None:
                    self._log.started(handle, command)
                self._log.record(self._agent_id, self._role, name, command, handle=handle)
                return HookResult()
            return self._checked(name, command or '', result)
        if name == 'BashOutput' and result.meta.get('running') is False:
            handle = result.meta.get('handle')
            command = self._log.first_finish(handle) if isinstance(handle, str) else None
            if command is not None:
                return self._checked(name, command, result)
        return HookResult()

    def _checked(self, name: str, command: str, result: ToolResult) -> HookResult:
        meta = result.meta
        exit_code = meta.get('exit_code')
        check = parse_check(
            command=command,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            stdout=str(meta.get('stdout', '')),
            stderr=str(meta.get('stderr', '')),
            timed_out=meta.get('timed_out') is True,
        )
        self._log.record(
            self._agent_id,
            self._role,
            name,
            command,
            exit_code if isinstance(exit_code, int) else None,
            check,
        )
        return HookResult(
            modified={'result': replace(result, meta={**meta, 'check': _check_meta(check)})}
        )

    async def _began(self, **kwargs: Any) -> HookResult:
        context = cast(Context, kwargs['ctx'])
        self._run = None
        self._start = len(context.current_traj)
        if not self._parent_known:
            try:
                info = await self._runtime.registry.get(self._agent_id)
            except Exception as exc:
                logger.warning('查不到 Agent %s 的父 Agent，记成没有父：%s', self._agent_id, exc)
            else:
                self._log.set_parent(self._agent_id, None if info is None else info.parent_id)
                self._parent_known = True
        start = await self._fingerprint('开始')
        self._run = self._log.begin(self._agent_id, self._role, start)
        return HookResult()

    async def _ended(self, **kwargs: Any) -> HookResult:
        context = cast(Context, kwargs['ctx'])
        end = await self._fingerprint('结束')
        if self._run is not None and context.stop_reason is not None:
            self._log.finish(
                self._run, context.stop_reason.value, last_answer(context, self._start), end
            )
        return HookResult()

    async def _failed(self, **kwargs: Any) -> HookResult:
        del kwargs
        if self._run is not None:
            self._log.finish(self._run, 'error', None, None)
        return HookResult()

    async def _fingerprint(self, moment: str) -> Fingerprint | None:
        if self._git is None:
            return None
        try:
            changes = await asyncio.to_thread(self._git.changes)
        except Exception as exc:
            logger.warning(
                'Reviewer %s 运行%s时的工作树指纹没取到，它这次的结论按 unverified 处理：%s',
                self._agent_id,
                moment,
                exc,
            )
            return Fingerprint(None, f'{type(exc).__name__}: {exc}')
        return Fingerprint(changes)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


class DevModule(RuntimeModule):
    """每个 Agent 一个拦截插件与一个记录插件，每个 Runtime 一份 `dev_log`。`workspace_root` 在构造时
    解析一次，规则不读 cwd；`environ` 是宿主自己的环境，取 Reviewer 指纹的 git 在它上面跑
    （D14）。"""

    def __init__(
        self,
        workspace_root: Path,
        environ: Mapping[str, str],
        *,
        reviewer_roles: Collection[str] = ('Reviewer',),
    ) -> None:
        if not isinstance(cast(object, environ), Mapping):
            raise ConfigError(
                expected='environ 是宿主环境的 Mapping（通常是 os.environ）',
                received=f'收到 {type(environ).__name__}',
                next_action='传 os.environ 或一个 dict；取 Reviewer 指纹的 git 在它上面跑',
            )
        # 一个字符串也是字符串的集合：`'Reviewer'` 会变成八个单字母角色，Reviewer 从此不取指纹。
        if isinstance(cast(object, reviewer_roles), str):
            raise ConfigError(
                expected='reviewer_roles 是角色名的集合',
                received=f'收到一个字符串 {reviewer_roles!r}',
                next_action=f'写成 ({reviewer_roles!r},)',
            )
        judge = dev_guard_judge(workspace_root)
        reviewers = frozenset(reviewer_roles)
        git = HostGit(workspace_root, environ)

        def guard(runtime: object, agent: object, flags: object) -> _Guard:
            del runtime, agent, flags
            return _Guard(judge)

        def recorder(runtime: object, agent: object, flags: object) -> _Recorder:
            del flags
            host = cast(Runtime, runtime)
            member = cast(BaseAgent, agent)
            # 服务是装配时 setattr 上去的，Runtime 的静态类型里没有这个属性。
            log = cast(DevLog, getattr(host, 'dev_log'))  # noqa: B009
            role = member.options.name
            # 登记在工厂里做：它同步执行，早于这个 Agent 的任务开始；AGENT_SPAWN 不带角色，而且
            # 在子 Agent 已经开始跑之后才触发。
            log.enroll(member.agent_id, role)
            return _Recorder(log, host, member.agent_id, role, git if role in reviewers else None)

        def dev_log(runtime: object, flags: object) -> DevLog:
            del runtime, flags
            return DevLog()

        super().__init__(
            'dev',
            agent_plugins=(guard, recorder),
            services={'dev_log': dev_log},
            version=__version__,
            description='开发期拦截 git 写操作与越界写入，记录每个 Agent 做了什么',
            capabilities=('environment', 'subprocess'),
            security_notes='用宿主环境跑 git，取 Reviewer 看到的工作区指纹；不改仓库。',
        )
