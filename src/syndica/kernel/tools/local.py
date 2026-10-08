"""本地文件、搜索、命令与终端工具。只调 Portal，不碰磁盘。

模型犯的错（越界、旧串不唯一、文件过大）写进 output，不抛。
这些工具的文本不是要执行的命令，所以 `__tool_command_args__` 必须是 `()`：
保持默认 None 时，危险判定会把文件正文当成命令读。
"""

from __future__ import annotations

import math
import posixpath
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import replace
from typing import cast

from syndica.kernel.artifacts import KINDS
from syndica.kernel.call_limit import wall_seconds_left
from syndica.kernel.clipping import clip_head, clip_tail
from syndica.kernel.context import ArtifactRef, TrustLevel
from syndica.kernel.driving import driving_agent
from syndica.kernel.errors import ConfigError
from syndica.kernel.file_taint import TaintAware
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.workspace import ExecResult, Portal, WriteConflict

__all__ = [
    'Bash',
    'BashOutput',
    'Deliver',
    'Edit',
    'Glob',
    'Grep',
    'KillBash',
    'Read',
    'Terminal',
    'Write',
    'local_tools',
]

_READ_LIMIT: int = 2000
# 截断时每条流保留的开头占比，其余留给结尾：pytest、ruff 的汇总都在输出末尾。
_HEAD_SHARE: float = 0.2


def _told(exc: ConfigError) -> ToolResult:
    return ToolResult(output=exc.message)


# 只按扩展名粗分；格式语义（预览、语义 diff、校验）归 M7 的产物处理器。
_KINDS: dict[str, tuple[str, str]] = {
    '.md': ('document', 'text/markdown'),
    '.txt': ('document', 'text/plain'),
    '.html': ('document', 'text/html'),
    '.csv': ('spreadsheet', 'text/csv'),
    '.tsv': ('spreadsheet', 'text/tab-separated-values'),
    '.json': ('data', 'application/json'),
    '.yaml': ('data', 'application/yaml'),
    '.yml': ('data', 'application/yaml'),
    '.toml': ('data', 'application/toml'),
    '.py': ('code', 'text/x-python'),
    '.js': ('code', 'text/javascript'),
    '.ts': ('code', 'text/x-typescript'),
    '.sh': ('code', 'text/x-shellscript'),
    '.svg': ('diagram', 'image/svg+xml'),
    '.mmd': ('diagram', 'text/vnd.mermaid'),
}


def routed_kind(path: str) -> tuple[str, str]:
    """按扩展名给出 (kind, mime)：L1 按它选处理器，`Deliver` 自选的 kind 只是标签。"""
    normal = posixpath.normpath(path)
    return _KINDS.get(posixpath.splitext(normal)[1].lower(), ('file', 'application/octet-stream'))


def _artifact(path: str, version: str) -> ArtifactRef:
    """`id` 由规范化的路径派生：同一个文件的各个版本是同一个产物，与谁写的无关（spec §7.2）。"""
    normal = posixpath.normpath(path)
    kind, mime = routed_kind(normal)
    return ArtifactRef(
        id=f'file:{normal}',
        path=normal,
        kind=kind,
        mime=mime,
        version=version,
        producer=driving_agent() or 'host',
    )


def _files_trust(portal: Portal, paths: Iterable[str]) -> TrustLevel:
    """列出来的这些路径里最严的那个（文件名也是数据：不可信的写入建出来的文件、或打开后新建的符号
    链接，名字同样不可信——哪怕链接指向一个基线文件）。"""
    if not isinstance(portal, TaintAware):
        return TrustLevel.TRUSTED
    for path in dict.fromkeys(paths):
        if portal.name_trust(path) is TrustLevel.UNTRUSTED:
            return TrustLevel.UNTRUSTED
    return TrustLevel.TRUSTED


def _conflicted(conflict: WriteConflict) -> ToolResult:
    """冲突是可恢复的结果：模型读到它就去重读合并或换个路径，不是一次失败的调用。"""
    return ToolResult(
        output=str(conflict),
        meta={
            'conflict': {
                'path': conflict.path,
                'expected': conflict.expected,
                'current': conflict.current,
                'unread': conflict.unread,
            }
        },
    )


def _valid_output_limit(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            expected=(
                'output_limit 是正整数（每条输出流、每次搜索结果最多展示的字符数），'
                '或 None 表示不截断'
            ),
            received=f'output_limit={value!r}，类型是 {type(value).__name__}',
            next_action='传一个大于 0 的 int，例如 10_000；不要传布尔值或浮点数',
        )
    return value


def _clipped(text: str, limit: int | None) -> tuple[str, bool]:
    """(展示给模型的文本, 是否截断)。stdout 与 stderr 各自截，不截拼好的整段。

    按流截是承重的：pytest 的汇总在 stdout 末尾，而拼好的文本里 stderr 排在最后，截整段会先把
    stdout 的结尾截掉。
    """
    if limit is None or len(text) <= limit:
        return text, False
    shown_head = clip_head(text, int(limit * _HEAD_SHARE))
    shown_tail = clip_tail(text, limit - int(limit * _HEAD_SHARE))
    omitted = len(text) - len(shown_head) - len(shown_tail)
    marker = (
        f'\n…[这条流的中间截掉了 {omitted} 个字符。要看被截掉的部分，把命令输出重定向到工作区里的'
        "文件，再用 grep、sed -n '起,止p' 或 tail 分段看]…\n"
    )
    return shown_head + marker + shown_tail, True


def _listed(items: Sequence[str], limit: int | None, narrow: str) -> list[str]:
    """搜索结果交给模型：总字符数超过上限时按顺序只列放得下的几条，最后一条写明共几条、怎么缩小。

    一次宽泛的搜索就能把整个目录的匹配行塞进上下文，此后这个 Agent 的每一次请求都带着它们。
    第一条就放不下时截短它而不是一条都不给，并说明它被截了：缩小搜索范围不会让一行变短。
    上限按结果本身的字符数算，后端编码时加的引号与转义不算在内，所以它是近似的。
    """
    if limit is None:
        return list(items)
    shown: list[str] = []
    room = limit
    for item in items:
        if len(item) > room:
            break
        shown.append(item)
        room -= len(item)
    else:
        return shown
    if shown:
        return [
            *shown,
            f'…[共 {len(items)} 条，只列出前 {len(shown)} 条：结果超过 {limit} 个字符的上限。'
            f'{narrow}]',
        ]
    first = items[0]
    rest = f'共 {len(items)} 条，其余没有列出：{narrow}' if len(items) > 1 else ''
    return [
        f'{clip_head(first, limit)}…',
        f'…[第一条结果有 {len(first)} 个字符，只显示了前 {limit} 个，要看全文用 Read 读它所在的'
        f'文件。{rest}]',
    ]


def _reported(
    result: ExecResult,
    limit: int | None,
    *,
    meta: dict[str, object],
    note: str = '',
    complete: ExecResult | None = None,
) -> ToolResult:
    """把一次命令的结果交给模型。`meta` 里的两条流是展示出来的那两条（可能已截断）；给了 `complete`
    时是它的两条：命令跑完之后读 meta 的人（开发期记录按输出判定测试结果）要的是全部输出，而不是
    这一次的增量。"""
    stdout, stdout_cut = _clipped(result.stdout, limit)
    stderr, stderr_cut = _clipped(result.stderr, limit)
    meta_stdout = stdout if complete is None else _clipped(complete.stdout, limit)[0]
    meta_stderr = stderr if complete is None else _clipped(complete.stderr, limit)[0]
    truncated = result.truncated or stdout_cut or stderr_cut
    shown = (
        f'{note}'
        f'running: {str(result.running).lower()}\n'
        f'exit_code: {result.exit_code}\n'
        f'truncated: {str(truncated).lower()}\n'
        f'timed_out: {str(result.timed_out).lower()}\n'
        f'stdout:\n{stdout}\n'
        f'stderr:\n{stderr}'
    )
    return ToolResult(
        output=shown,
        meta={
            **meta,
            'exit_code': result.exit_code,
            'running': result.running,
            'truncated': truncated,
            'timed_out': result.timed_out,
            'stdout': meta_stdout,
            'stderr': meta_stderr,
        },
        trust=TrustLevel.UNTRUSTED,
    )


# 与后台取消同一句：到点停下，不声称命令没产生副作用，也不声称它做完了。
_BUDGET_STOPPED = '到预算截止时刻停下，副作用未知：它可能已经做了一部分。需要它的结果就重新发起。'
# 没起过的命令不能套上一句：「可能做了一部分」会让模型去查、去补一个不存在的副作用。
_BUDGET_NOT_STARTED = '没有运行：预算截止时刻已经过了，这条命令没有启动，没有任何副作用。'


def _valid_timeout(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(
            expected='timeout 是有限正数秒数',
            received=f'timeout={value!r}，类型是 {type(value).__name__}',
            next_action='传入大于 0 的 int 或 float，不要传布尔值',
        )
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ConfigError(
            expected='timeout 是有限正数秒数',
            received=f'timeout={value!r}',
            next_action='传入大于 0 的有限秒数；0、负数、inf 与 nan 都不合法',
        )


def _valid_background(value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            expected='run_in_background 是 bool',
            received=f'run_in_background={value!r}，类型是 {type(value).__name__}',
            next_action='传 True 启动后台命令，或传 False 等待前台命令完成',
        )
    return value


class Read(Tool):
    """读工作区里的一个文件。模型看到的描述来自 execute 的 docstring。"""

    __tool_name__ = 'Read'
    __tool_command_args__ = ()
    __tool_external_input__ = True

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, path: str, offset: int = 1, limit: int = _READ_LIMIT) -> ToolResult:
        """读取工作区里的一个文本文件。
        path: 相对工作区根的路径
        offset: 从 1 开始的行号，默认 1
        limit: 最多返回的行数，默认 2000
        """
        try:
            text, version = await self._portal.read_versioned(path, offset=offset, limit=limit)
        except ConfigError as exc:
            return _told(exc)
        trust = (
            self._portal.file_trust(path, version)
            if isinstance(self._portal, TaintAware)
            else TrustLevel.TRUSTED
        )
        return ToolResult(output=text, trust=trust)


class Write(Tool):
    """覆盖写入工作区里的一个文件。"""

    __tool_name__ = 'Write'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, path: str, content: str) -> ToolResult:
        """覆盖写入一个文件。父目录不存在时会创建，但必须仍在工作区根内。
        path: 相对工作区根的路径
        content: 写入的全文
        """
        try:
            version = await self._portal.write_text(path, content)
        except ConfigError as exc:
            return _told(exc)
        except WriteConflict as conflict:
            return _conflicted(conflict)
        # `meta['path']` 只在写成了时有：开发期记录靠它分辨写成的与被拒的（被拒的文字也是一段话）。
        return ToolResult(
            output=f'已写入 {path}', meta={'path': path}, artifacts=[_artifact(path, version)]
        )


class Edit(Tool):
    """精确替换工作区里出现恰好一次的一段文本。"""

    __tool_name__ = 'Edit'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, path: str, old_string: str, new_string: str) -> ToolResult:
        """把文件里出现恰好一次的一段文本换成另一段。出现 0 次或多于 1 次都不会改文件。
        path: 相对工作区根的路径
        old_string: 要被替换的原文，必须非空且在文件里恰好出现一次
        new_string: 替换后的文本
        """
        try:
            version = await self._portal.edit_text(path, old_string, new_string)
        except ConfigError as exc:
            return _told(exc)
        except WriteConflict as conflict:
            return _conflicted(conflict)
        return ToolResult(
            output=f'已替换 {path} 中的一处文本',
            meta={'path': path},
            artifacts=[_artifact(path, version)],
        )


class Deliver(Tool):
    """把工作区里的一个文件显式标记为交付物（spec §7.4）。"""

    __tool_name__ = 'Deliver'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(
        self, portal: Portal, *, kinds: Callable[[], Collection[str]] | None = None
    ) -> None:
        self._portal = portal
        # 每次调用再取：处理器可以在工具建好之后才装上。
        self._kinds = kinds if kinds is not None else (lambda: KINDS)

    async def execute(self, path: str, note: str = '', kind: str = '') -> ToolResult:
        """把一个已经写出的文件标记为交付物。验收只看交付物，不查临时脚本 / 中间数据，所以这是
        控制成本的手段：把真正要交付的东西挑出来。
        path: 相对工作区根的路径，必须已存在
        note: 这是什么 / 为什么交付它（可空）
        kind: 覆盖语义类别（document / spreadsheet / diagram / data / code，可空，默认按扩展名）
        """
        try:
            stat = await self._portal.stat(path)
        except ConfigError as exc:
            return _told(exc)
        if stat is None:
            return ToolResult(
                output=f'交付失败：{path} 不存在。先写出这个文件再 Deliver，或检查路径是否写对。'
            )
        accepted = frozenset(self._kinds())
        if kind and kind not in accepted:
            # 模型自选的 kind 决定交给哪个处理器做 L1：放任自由文本就能把交付物绕过严格的处理器。
            return ToolResult(
                output=(
                    f'交付失败：kind={kind!r} 不认识。期望 {", ".join(sorted(accepted))} 之一；'
                    '拿不准就留空，按扩展名推断。'
                )
            )
        ref = _artifact(path, stat.version)
        ref = replace(ref, kind=kind or ref.kind, is_deliverable=True)
        return ToolResult(
            output=f'已标记交付物：{path}', meta={'path': path, 'note': note}, artifacts=[ref]
        )


class Glob(Tool):
    """按模式查找工作区里的路径。"""

    __tool_name__ = 'Glob'
    __tool_command_args__ = ()
    __tool_external_input__ = True

    def __init__(self, portal: Portal, *, output_limit: int | None = None) -> None:
        self._portal = portal
        self._output_limit = _valid_output_limit(output_limit)

    async def execute(self, pattern: str) -> ToolResult:
        """按相对工作区根的模式查找路径，结果按字典序排列。
        结果太多时可能只列出前面一部分，并写明共几条；以 . 开头的目录与文件可能不列，要找它们就在
        pattern 里写出来（例如 .github/**）。
        pattern: 相对工作区根的 glob 模式
        """
        try:
            paths = await self._portal.glob_paths(pattern)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(
            output=_listed(paths, self._output_limit, '缩小 pattern 再找。'),
            trust=_files_trust(self._portal, paths),
        )


class Grep(Tool):
    """按正则搜索工作区里的文本。"""

    __tool_name__ = 'Grep'
    __tool_command_args__ = ()
    __tool_external_input__ = True

    def __init__(self, portal: Portal, *, output_limit: int | None = None) -> None:
        self._portal = portal
        self._output_limit = _valid_output_limit(output_limit)

    async def execute(self, pattern: str, path: str = '.') -> ToolResult:
        """按 Python 正则搜索文件，每条结果包含相对路径、行号和行内容。
        结果太多时可能只列出前面一部分，并写明共几条；以 . 开头的目录与文件、二进制文件可能不搜，
        要搜某个以 . 开头的目录或文件就把它写进 path。
        pattern: Python 正则表达式
        path: 相对工作区根的文件或目录，默认搜索整个工作区
        """
        try:
            hits = await self._portal.grep_text(pattern, path)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(
            output=_listed(
                [str(hit) for hit in hits], self._output_limit, '缩小 pattern 或 path 再搜。'
            ),
            trust=_files_trust(self._portal, (hit.path for hit in hits)),
        )


class Bash(Tool):
    """在工作区里运行一条 shell 命令。"""

    __tool_name__ = 'Bash'
    __tool_command_args__ = ('command',)
    __tool_external_input__ = True
    __tool_backgroundable__ = True

    def __init__(self, portal: Portal, *, output_limit: int | None = None) -> None:
        self._portal = portal
        self._output_limit = _valid_output_limit(output_limit)

    async def execute(
        self,
        command: str,
        timeout: float = 30.0,
        cwd: str = '.',
        run_in_background: bool = False,
    ) -> ToolResult:
        """在工作区目录内运行 shell 命令，可选择转入后台。stdin 是空的。

        命令的 shell 一退出，它用 & 或 nohup 留下的进程最多再跑约 1 秒就被终止，前台后台都一样。
        要长期运行一个程序，让它本身就是这条命令并设 run_in_background，命令里不要再加 &。
        command: 要执行的 shell 命令
        timeout: 前台运行的超时秒数，默认 30；超时时终止整个进程组，交回已经收到的输出
        cwd: 相对工作区根的工作目录，默认根目录；不能在 .git 里
        run_in_background: 是否立即返回后台句柄
        """
        try:
            _valid_timeout(timeout)
            background = _valid_background(run_in_background)
            if background:
                handle = await self._portal.start(command, cwd=cwd)
                return ToolResult(
                    output=f'后台命令已启动，handle={handle}',
                    meta={'handle': handle},
                )
            left = wall_seconds_left()
            clamped = left is not None and left < float(timeout)
            limit = float(timeout) if not clamped else cast(float, left)
            if clamped and limit <= 0:
                return ToolResult(output=_BUDGET_NOT_STARTED)
            result = await self._portal.run(command, timeout=limit, cwd=cwd)
        except ConfigError as exc:
            return _told(exc)
        if result.timed_out and clamped:
            note = _BUDGET_STOPPED + '\n'
        else:
            note = (
                f'命令超时：运行超过 {float(timeout):g} 秒，已终止整个进程组；'
                '下面是终止前收到的输出。\n'
                if result.timed_out
                else ''
            )
        return _reported(result, self._output_limit, meta={}, note=note)


class BashOutput(Tool):
    """查询一个后台命令的状态与输出。"""

    __tool_name__ = 'BashOutput'
    __tool_command_args__ = ()
    __tool_external_input__ = True

    def __init__(self, portal: Portal, *, output_limit: int | None = None) -> None:
        self._portal = portal
        self._output_limit = _valid_output_limit(output_limit)
        # 每个 Agent 对每个句柄读到了哪里：同一个后台命令被两个 Agent 查询时各自的增量互不串线。
        self._read: dict[tuple[str | None, str], tuple[int, int]] = {}

    async def execute(self, handle: str, full: bool = False) -> ToolResult:
        """查询后台命令；仍在运行时不等待它结束。缺省只交回你上次查询之后的新输出。
        handle: Bash 启动后台命令后返回的句柄
        full: 为 true 时交回到目前为止的全部输出，默认 false
        """
        if not isinstance(cast(object, full), bool):
            return ToolResult(
                output=(
                    f'期望: full 是 true 或 false。实际: {full!r}。'
                    ' 下一步: 只看新输出就不传 full；要从头看传 true。'
                )
            )
        key = (driving_agent(), handle)
        since = (0, 0) if full else self._read.get(key, (0, 0))
        try:
            result = await self._portal.poll(handle, since=since)
            complete = (
                await self._portal.poll(handle) if since != (0, 0) and not result.running else None
            )
        except ConfigError as exc:
            return _told(exc)
        self._read[key] = (result.stdout_end, result.stderr_end)
        note = '下面只是你上次查询之后的新输出；要从头看传 full=true。\n' if since != (0, 0) else ''
        return _reported(
            result, self._output_limit, meta={'handle': handle}, note=note, complete=complete
        )


class KillBash(Tool):
    """终止一个后台命令的整个进程组。"""

    __tool_name__ = 'KillBash'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, handle: str) -> ToolResult:
        """终止后台命令及其子进程。
        handle: Bash 启动后台命令后返回的句柄
        """
        try:
            await self._portal.kill(handle)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(output=f'已终止后台命令，handle={handle}', meta={'handle': handle})


_TERMINAL_ACTIONS = ('create', 'send', 'capture', 'resize', 'kill', 'status')
# 能单独发的按键。行编辑键（Ctrl-W、退格、方向键）刻意不在这里：终端会按它们改写正在输入的那一行，
# 危险判定读到的文字于是不是 shell 真正执行的那一行。ctrl-c 后面紧跟一个 Ctrl-U：忽略 SIGINT 的程序
# 收到中断不会丢掉 readline 里那半行，Ctrl-U 不管信号怎么处理都把它清掉（光标只可能在行尾，
# 因为挪光标的键都不收），本工具于是能在 ctrl-c 之后把半行记成空的。
_TERMINAL_KEYS = {'ctrl-c': '\x03\x15', 'ctrl-d': '\x04', 'ctrl-z': '\x1a'}
# input 里不收的字符：除换行、回车之外的 C0、DEL 与 C1。它们会被终端的行规程或行编辑器当成编辑或
# 补全指令（`\x15git push` 执行的是 `git push`，`gi<TAB> push` 被补成什么判定看不见）。
_LINE_EDITING = re.compile('[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f]')


class Terminal(Tool):
    """在伪终端里运行交互式程序（spec §6.5）。

    危险判定读的是 `command` 与 `input` 的原文，所以本工具保证终端真正交给程序的每一行都完整地
    出现在某一次调用的 `input` 里：`input` 不收控制字符（行编辑与补全）；一次带回车的 send 之前，
    终端里不能还留着上一次 send 没回车的半行（否则提交的是「半行 + 这次的文字」，两半分别被判定过，
    合起来没被判定过）。半行按会话记，不按 Agent：终端的行缓冲只有一份。
    """

    __tool_name__ = 'Terminal'
    # `create` 的命令与 `send` 写进终端的文字都会被 shell 或终端里的程序执行。
    __tool_command_args__ = ('command', 'input')
    # 伪终端跨 send 保持 cwd：上一次 send 的 cd 留到下一次，危险判定认不出此刻的 cwd（评审 C7）。
    __tool_session_cwd__ = True
    __tool_external_input__ = True

    def __init__(self, portal: Portal, *, output_limit: int | None = None) -> None:
        self._portal = portal
        self._output_limit = _valid_output_limit(output_limit)
        # 每个 Agent 对每个会话看到了哪里，理由同 BashOutput。
        self._shown: dict[tuple[str | None, str], int] = {}
        self._pending: dict[str, str] = {}

    async def execute(
        self,
        action: str,
        command: str = '',
        handle: str = '',
        input: str = '',  # noqa: A002
        key: str = '',
        wait: float = 2.0,
        cols: int = 120,
        rows: int = 40,
    ) -> ToolResult:
        """在伪终端里运行需要终端的交互式程序（REPL、等你输入的提示、按键驱动的程序）。
        只是跑一条命令、看它的输出，用 Bash。终端是输出流，不是渲染后的屏幕：颜色与光标控制已去掉。
        create 用 command 起一个会话，交回 handle 与最初的输出；send 把 input 或 key 写进会话，
        交回之后的新输出；capture 交回上次之后的新输出；resize 改窗口大小；kill 结束会话；
        status 只看状态。
        input 不能含制表等控制字符，一行要在一次 send 里交完；key 是 ctrl-c（中断，并清掉没回车的
        半行）、ctrl-d（结束输入）或 ctrl-z（挂起，不清半行）。
        action: create / send / capture / resize / kill / status
        command: create 时运行的 shell 命令
        handle: create 交回的会话句柄，create 以外的动作都要
        input: send 写进终端的文字，回车写 \\n
        key: send 时单独发的一个键：ctrl-c / ctrl-d / ctrl-z
        wait: create、send、capture 之后最多等多少秒新输出，默认 2；到点没有新输出就交回空的
        cols: create 与 resize 的窗口列数，默认 120
        rows: create 与 resize 的窗口行数，默认 40
        """
        if action not in _TERMINAL_ACTIONS:
            return ToolResult(
                output=(
                    f'期望: action 是 {" / ".join(_TERMINAL_ACTIONS)} 之一。实际: {action!r}。'
                    ' 下一步: 先 create 一个会话，再用它交回的 handle 做别的动作。'
                )
            )
        try:
            if action == 'create':
                handle = await self._portal.pty_create(command, cwd='.', cols=cols, rows=rows)
                self._pending[handle] = ''
            elif action == 'send':
                refused = self._refused(handle, input, key)
                if refused is not None:
                    return refused
                await self._portal.pty_send(handle, _TERMINAL_KEYS[key] if key else input)
                self._remember(handle, input, key)
            elif action == 'resize':
                await self._portal.pty_resize(handle, cols=cols, rows=rows)
            elif action == 'kill':
                await self._portal.pty_kill(handle)
                for seen in [seen for seen in self._shown if seen[1] == handle]:
                    del self._shown[seen]
                self._pending.pop(handle, None)
                return ToolResult(
                    output=f'已结束终端会话，handle={handle}', meta={'handle': handle}
                )
            if action in ('resize', 'status'):
                status = await self._portal.pty_status(handle)
                return ToolResult(
                    output=(
                        f'handle: {handle}\nrunning: {str(status.running).lower()}\n'
                        f'exit_code: {status.exit_code}\nsize: {status.cols}x{status.rows}'
                    ),
                    meta={
                        'handle': handle,
                        'running': status.running,
                        'exit_code': status.exit_code,
                    },
                )
            captured = await self._portal.pty_capture(
                handle, since=self._shown.get((driving_agent(), handle), 0), wait=wait
            )
        except ConfigError as exc:
            return _told(exc)
        self._shown[(driving_agent(), handle)] = captured.end
        text, clipped = _clipped(captured.text, self._output_limit)
        return ToolResult(
            output=(
                f'handle: {handle}\nrunning: {str(captured.running).lower()}\n'
                f'exit_code: {captured.exit_code}\n'
                f'truncated: {str(captured.truncated or clipped).lower()}\n'
                f'output since last look:\n{text}'
            ),
            meta={
                'handle': handle,
                'running': captured.running,
                'exit_code': captured.exit_code,
                'output': text,
            },
            trust=TrustLevel.UNTRUSTED,
        )

    def _refused(self, handle: str, text: str, key: str) -> ToolResult | None:
        pending = self._pending.get(handle, '')
        if key:
            if key not in _TERMINAL_KEYS:
                return ToolResult(
                    output=(
                        f'期望: key 是 {" / ".join(_TERMINAL_KEYS)} 之一。实际: {key!r}。'
                        ' 下一步: 文字与回车放进 input；'
                        '改正在输入的一行时先 key=ctrl-c 清掉再整行重发。'
                    )
                )
            if text:
                return ToolResult(
                    output=(
                        '期望: key 单独发。实际: 同一次 send 里还带了 input。'
                        ' 下一步: 分成两次 send，先发文字、再发按键（或反过来）。'
                    )
                )
            if key == 'ctrl-d' and pending:
                return ToolResult(
                    output=(
                        f'期望: 发 ctrl-d 时终端里没有没回车的半行。实际: 还留着 {pending!r}，'
                        'ctrl-d 会把它当成一整行交给程序。'
                        ' 下一步: 先 key=ctrl-c 清掉它，或者把整行补完重新发。'
                    )
                )
            return None
        found = _LINE_EDITING.search(text)
        if found is not None:
            return ToolResult(
                output=(
                    f'期望: input 只含文字、空格与换行。实际: 第 {found.start() + 1} 个字符是'
                    f'控制字符 U+{ord(found.group()):04X}，终端会把它当成编辑或补全指令。'
                    ' 下一步: 回车写 \\n，缩进用空格；中断、结束输入、挂起用 key；'
                    '要改正在输入的一行，先 key=ctrl-c 清掉再整行重发。'
                )
            )
        if pending and ('\n' in text or '\r' in text):
            return ToolResult(
                output=(
                    '期望: 一行在一次 send 里交完。'
                    f'实际: 终端里还留着上一次没回车的半行 {pending!r}，'
                    '这次的回车会把它和这次的文字拼成一行提交。'
                    ' 下一步: 先 key=ctrl-c 清掉那半行，再把整行放进一次 send。'
                )
            )
        return None

    def _remember(self, handle: str, text: str, key: str) -> None:
        if key == 'ctrl-c':
            self._pending[handle] = ''
            return
        if key:
            # ctrl-z 不清：交互式 shell 在提示符下忽略挂起，readline 里那半行原样留着。
            return
        end = max(text.rfind('\n'), text.rfind('\r'))
        pending = self._pending.get(handle, '')
        self._pending[handle] = text[end + 1 :] if end >= 0 else pending + text


def local_tools(
    portal: Portal,
    *,
    output_limit: int | None = None,
    artifact_kinds: Callable[[], Collection[str]] | None = None,
) -> dict[str, Tool]:
    """按固定顺序交出本地工具。后续任务只追加键，不改已有键的相对顺序。

    `output_limit` 是 Bash、BashOutput 与 Terminal 每条输出、Glob 与 Grep 每次结果最多展示的
    字符数，None 是不截断。`artifact_kinds` 交回 `Deliver` 收的产物类别（宿主装了处理器时传它们
    声明的类别），不给就只收内核的基础类别。
    """
    limit = _valid_output_limit(output_limit)
    read = Read(portal)
    write = Write(portal)
    edit = Edit(portal)
    glob = Glob(portal, output_limit=limit)
    grep = Grep(portal, output_limit=limit)
    bash = Bash(portal, output_limit=limit)
    bash_output = BashOutput(portal, output_limit=limit)
    kill_bash = KillBash(portal)
    terminal = Terminal(portal, output_limit=limit)
    deliver = Deliver(portal, kinds=artifact_kinds)
    return {
        read.name: read,
        write.name: write,
        edit.name: edit,
        glob.name: glob,
        grep.name: grep,
        bash.name: bash,
        bash_output.name: bash_output,
        kill_bash.name: kill_bash,
        terminal.name: terminal,
        deliver.name: deliver,
    }
