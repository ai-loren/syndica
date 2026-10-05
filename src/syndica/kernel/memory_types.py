"""分层记忆的可序列化状态（M6 §8.9）。

两层次：全局记忆（`GLOBAL`，跨所有工作）与工作区级记忆（`WORKSPACE`，特定项目）。两阶段：短期
`MemoryEntry`（「最近发生了什么」，按 `seq` 记）累积到阈值后整理成结构化的长期 `MemorySection`。
`through_seq` 记整理到了哪条短期，整理只读到它、不漏不重。`version` 供整理做乐观并发（§6.4：冲突
显式返回而非静默覆盖）。

信任是承重轴：不可信内容**永不进全局**（§8.9，§12.2）。这条由 `memory_redaction` 在铸造时强制，store
只收铸造过的类型，第三方实现也绕不过。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from syndica.kernel.context import TrustLevel

__all__ = ['MemoryEntry', 'MemoryRecord', 'MemoryScope', 'MemorySection']


class MemoryScope(Enum):
    """记忆的层次。`GLOBAL` 跨所有工作区；`WORKSPACE` 只属于一个工作区。取值逐字落盘，改即破坏。"""

    GLOBAL = 'global'
    WORKSPACE = 'workspace'


@dataclass
class MemoryEntry:
    """一条短期记忆：某一轮里值得记住的一件事。`seq` 由 store 按 (scope, workspace) 单调发号。"""

    seq: int
    scope: MemoryScope
    workspace: str | None
    text: str
    trust: TrustLevel = TrustLevel.TRUSTED
    created_at: float = field(default_factory=time.time)


@dataclass
class MemorySection:
    """一段整理过的长期记忆。`trust` 随内容：任一来源不可信则这一段不可信（strictest）。"""

    heading: str
    text: str
    trust: TrustLevel = TrustLevel.TRUSTED


@dataclass
class MemoryRecord:
    """某 (scope, workspace) 的长期记忆：整理出的段落、整理到哪条短期（`through_seq`）、乐观并发的
    `version`（每次 consolidate +1）。"""

    scope: MemoryScope
    workspace: str | None
    sections: list[MemorySection] = field(default_factory=list[MemorySection])
    through_seq: int = 0
    version: int = 0
    updated_at: float = field(default_factory=time.time)
