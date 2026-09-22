"""本地文件工具。只调 Portal，不碰磁盘。

模型犯的错（越界、旧串不唯一、文件过大）写进 output，不抛。
这些工具的文本不是要执行的命令，所以 `__tool_command_args__` 必须是 `()`：
保持默认 None 时，危险判定会把文件正文当成命令读。
"""

from __future__ import annotations

from syndica.kernel.errors import ConfigError
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.workspace import Portal

__all__ = ['Edit', 'Read', 'Write', 'local_tools']

_READ_LIMIT: int = 2000


def _told(exc: ConfigError) -> ToolResult:
    return ToolResult(output=exc.message)


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


def local_tools(portal: Portal) -> dict[str, Tool]:
    """按固定顺序交出本地工具。后续任务只追加键，不改已有键的相对顺序。"""
    read = Read(portal)
    write = Write(portal)
    edit = Edit(portal)
    return {read.name: read, write.name: write, edit.name: edit}
