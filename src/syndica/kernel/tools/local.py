"""本地文件与搜索工具。只调 Portal，不碰磁盘。

模型犯的错（越界、旧串不唯一、文件过大）写进 output，不抛。
这些工具的文本不是要执行的命令，所以 `__tool_command_args__` 必须是 `()`：
保持默认 None 时，危险判定会把文件正文当成命令读。
"""

from __future__ import annotations

import math

from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.workspace import ExecResult, Portal

__all__ = [
    'Bash',
    'BashOutput',
    'Edit',
    'Glob',
    'Grep',
    'KillBash',
    'Read',
    'Write',
    'local_tools',
]

_READ_LIMIT: int = 2000


def _told(exc: ConfigError) -> ToolResult:
    return ToolResult(output=exc.message)


def _shown(result: ExecResult) -> str:
    return (
        f'running: {str(result.running).lower()}\n'
        f'exit_code: {result.exit_code}\n'
        f'truncated: {str(result.truncated).lower()}\n'
        f'stdout:\n{result.stdout}\n'
        f'stderr:\n{result.stderr}'
    )


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

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, path: str, offset: int = 1, limit: int = _READ_LIMIT) -> ToolResult:
        """读取工作区里的一个文本文件。
        path: 相对工作区根的路径
        offset: 从 1 开始的行号，默认 1
        limit: 最多返回的行数，默认 2000
        """
        try:
            text = await self._portal.read_text(path, offset=offset, limit=limit)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(output=text)


class Write(Tool):
    """覆盖写入工作区里的一个文件。"""

    __tool_name__ = 'Write'
    __tool_command_args__ = ()

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, path: str, content: str) -> ToolResult:
        """覆盖写入一个文件。父目录不存在时会创建，但必须仍在工作区根内。
        path: 相对工作区根的路径
        content: 写入的全文
        """
        try:
            await self._portal.write_text(path, content)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(output=f'已写入 {path}')


class Edit(Tool):
    """精确替换工作区里出现恰好一次的一段文本。"""

    __tool_name__ = 'Edit'
    __tool_command_args__ = ()

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, path: str, old_string: str, new_string: str) -> ToolResult:
        """把文件里出现恰好一次的一段文本换成另一段。出现 0 次或多于 1 次都不会改文件。
        path: 相对工作区根的路径
        old_string: 要被替换的原文，必须非空且在文件里恰好出现一次
        new_string: 替换后的文本
        """
        try:
            await self._portal.edit_text(path, old_string, new_string)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(output=f'已替换 {path} 中的一处文本')


class Glob(Tool):
    """按模式查找工作区里的路径。"""

    __tool_name__ = 'Glob'
    __tool_command_args__ = ()

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, pattern: str) -> ToolResult:
        """按相对工作区根的模式查找路径，结果按字典序排列。
        pattern: 相对工作区根的 glob 模式
        """
        try:
            paths = await self._portal.glob_paths(pattern)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(output=list(paths))


class Grep(Tool):
    """按正则搜索工作区里的文本。"""

    __tool_name__ = 'Grep'
    __tool_command_args__ = ()

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, pattern: str, path: str = '.') -> ToolResult:
        """按 Python 正则搜索文件，每条结果包含相对路径、行号和行内容。
        pattern: Python 正则表达式
        path: 相对工作区根的文件或目录，默认搜索整个工作区
        """
        try:
            hits = await self._portal.grep_text(pattern, path)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(output=list(hits))


class Bash(Tool):
    """在工作区里运行一条 shell 命令。"""

    __tool_name__ = 'Bash'
    __tool_command_args__ = ('command',)

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(
        self,
        command: str,
        timeout: float = 30.0,
        cwd: str = '.',
        run_in_background: bool = False,
    ) -> ToolResult:
        """在工作区目录内运行 shell 命令，可选择转入后台。
        command: 要执行的 shell 命令
        timeout: 前台运行的超时秒数，默认 30
        cwd: 相对工作区根的工作目录，默认根目录
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
            result = await self._portal.run(command, timeout=timeout, cwd=cwd)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(output=_shown(result), trust=TrustLevel.UNTRUSTED)


class BashOutput(Tool):
    """查询一个后台命令的状态与输出。"""

    __tool_name__ = 'BashOutput'
    __tool_command_args__ = ()

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def execute(self, handle: str) -> ToolResult:
        """查询后台命令；仍在运行时不等待它结束。
        handle: Bash 启动后台命令后返回的句柄
        """
        try:
            result = await self._portal.poll(handle)
        except ConfigError as exc:
            return _told(exc)
        return ToolResult(
            output=_shown(result),
            meta={
                'handle': handle,
                'running': result.running,
                'exit_code': result.exit_code,
            },
            trust=TrustLevel.UNTRUSTED,
        )


class KillBash(Tool):
    """终止一个后台命令的整个进程组。"""

    __tool_name__ = 'KillBash'
    __tool_command_args__ = ()

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


def local_tools(portal: Portal) -> dict[str, Tool]:
    """按固定顺序交出本地工具。后续任务只追加键，不改已有键的相对顺序。"""
    read = Read(portal)
    write = Write(portal)
    edit = Edit(portal)
    glob = Glob(portal)
    grep = Grep(portal)
    bash = Bash(portal)
    bash_output = BashOutput(portal)
    kill_bash = KillBash(portal)
    return {
        read.name: read,
        write.name: write,
        edit.name: edit,
        glob.name: glob,
        grep.name: grep,
        bash.name: bash,
        bash_output.name: bash_output,
        kill_bash.name: kill_bash,
    }
