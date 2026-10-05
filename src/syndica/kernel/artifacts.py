"""产物处理器：格式语义（预览 / 语义 diff / 校验）的归属（spec §7.2）。

内核只认身份与版本，**格式语义全在处理器**。内核自带少量基础 `kind`（与 `tools/local.py` 的扩展名表
一致）和一个只做存在性 / 文本 diff 的 `generic` 处理器；二进制格式（xlsx / docx）的处理器归后端。

处理器按 `(kind, mime)` 建索引，**更具体优先**（`(k,m)` > `(k,'*')` > `('*',m)` > `('*','*')`）；
**同一精度重复注册启动即报错**——否则装了两个都声明处理表格的后端，行为取决于导入顺序。

方法都 `async` 且收一个绑定 Portal 的 `ArtifactReader`：处理器只拿到 `ArtifactRef`（路径 + 版本），
内容要按版本从工作区读，而 Portal 每个 Agent（每个隔离副本）一份；同步签名既读不到内容、大文件又会
阻塞事件循环（评审 H5 / P2）。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from syndica.kernel.context import ArtifactRef
from syndica.kernel.errors import ConfigError

__all__ = [
    'KINDS',
    'ArtifactHandler',
    'ArtifactHandlers',
    'ArtifactReader',
    'Diff',
    'GenericHandler',
    'Preview',
    'Violation',
]

# 内核自带的基础类别（与 tools/local.py 的 _KINDS 实产一致）。处理器可注册这之外的新类别（§7.2）。
KINDS = frozenset({'document', 'spreadsheet', 'data', 'code', 'diagram', 'file'})

_PREVIEW_CHARS = 2000


@dataclass(frozen=True)
class Preview:
    """产物的一段可读渲染。文本格式是开头若干字符；二进制由后端处理器给。"""

    text: str


@dataclass(frozen=True)
class Diff:
    """两个版本之间的语义差异。`summary` 是人读摘要；`changed` 为假表示两版等价。"""

    summary: str
    changed: bool = True


@dataclass(frozen=True)
class Violation:
    """一条 L1 格式违规（spec §7.5）。`anchor` 是指进产物的定位（如 `report.md#L3`），可为空。"""

    rule: str
    message: str
    anchor: str | None = None


@runtime_checkable
class ArtifactReader(Protocol):
    async def read(self, ref: ArtifactRef) -> str:
        """按 `ref.version` 从工作区读出产物内容。读不到（不存在 / 删了）抛，让处理器据此判违规。"""
        ...


@runtime_checkable
class ArtifactHandler(Protocol):
    handles: tuple[tuple[str, str], ...]

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview: ...

    async def semantic_diff(
        self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader
    ) -> Diff: ...

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]: ...


class GenericHandler:
    """兜底处理器：任何产物都能预览、diff、校验，但只按文本处理。注册为 `('*','*')`。"""

    handles: tuple[tuple[str, str], ...] = (('*', '*'),)

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:
        return Preview(text=(await reader.read(ref))[:_PREVIEW_CHARS])

    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Diff:
        before = (await reader.read(a)).splitlines()
        after = (await reader.read(b)).splitlines()
        if before == after:
            return Diff(summary='两个版本内容相同', changed=False)
        delta = sum(1 for line in difflib.ndiff(before, after) if line[0] in '+-')
        return Diff(summary=f'{a.version} → {b.version}：{delta} 行不同', changed=True)

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]:
        content = await reader.read(ref)
        if not content.strip():
            return [Violation(rule='empty', message=f'{ref.path} 是空的', anchor=ref.path)]
        return []


class ArtifactHandlers:
    """处理器注册表。`generic` 预注册为 `('*','*')`；更具体的匹配优先，同精度冲突构造时报错。"""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ArtifactHandler] = {}
        self.register(GenericHandler())

    def register(self, handler: ArtifactHandler) -> None:
        for kind, mime in handler.handles:
            key = (kind, mime)
            existing = self._by_key.get(key)
            if existing is not None:
                raise ConfigError(
                    expected=f'(kind, mime)={key} 只由一个处理器声明（spec §7.2）',
                    received=f'{_name(existing)} 与 {_name(handler)} 都声明了它',
                    next_action='卸掉其中一个，或用更具体的 (kind, mime) 区分；别让它看导入顺序',
                )
            self._by_key[key] = handler

    def for_(self, ref: ArtifactRef) -> ArtifactHandler:
        for key in ((ref.kind, ref.mime), (ref.kind, '*'), ('*', ref.mime), ('*', '*')):
            handler = self._by_key.get(key)
            if handler is not None:
                return handler
        return self._by_key[('*', '*')]  # generic 预注册，恒命中


def _name(handler: ArtifactHandler) -> str:
    cls = type(handler)
    return f'{cls.__module__}.{cls.__qualname__}'
