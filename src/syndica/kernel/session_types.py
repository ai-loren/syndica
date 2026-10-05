"""会话的可序列化状态（M6 §4.4）。

一次 `run` / `chat` 的根对话落盘成 `SessionRecord`：根 Agent 的轨迹，加上入队时快照的 system prompt
与 flags（恢复沿用快照，改 Agent 不影响在途会话，与 agent-service 的 Task 同理）。`SessionMeta` 是能
不读正文就答出来的那部分——正文可达数十兆，只判存在或只取元数据时不该把它读进内存（spec §4.4 三条
读写纪律之一）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from syndica.kernel.context import ContextBlock

__all__ = ['SessionMeta', 'SessionRecord', 'SessionStatus']


class SessionStatus(Enum):
    """会话此刻的归宿。`active` 还能接着跑，`archived` 是归过档的历史副本。"""

    ACTIVE = 'active'
    ARCHIVED = 'archived'


@dataclass
class SessionMeta:
    """不读正文就能答出来的会话元数据。字段顺序是公开契约，新字段只能追加。"""

    sid: str
    title: str = ''
    agent: str = ''
    workspace: str | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    turns: int = 0
    # 归档序号只增不补（spec §4.4）：永远取当前最大值 +1，出现空洞不回填。未归档是 0。
    archived: int = 0
    # 这个会话最近一次 run 的 trace id：恢复后还能顺着它回看上一段（路线图要求保留 trace link）。
    last_trace: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class SessionRecord:
    """一次会话的全部可序列化状态：元数据 + 根 Agent 的轨迹 + 入队时的前缀快照与 flags。"""

    meta: SessionMeta
    content: list[ContextBlock] = field(default_factory=list[ContextBlock])
    system_prompt: str = ''
    # 恢复只为缓存连续性沿用这份前缀快照（SP + 模型 + 工具名按序）：spec §5.2 次序属于前缀。
    # 安全相关 flags 恢复时另取「当前或更严」，不用快照里的旧值。
    model: str = ''
    tools: list[str] = field(default_factory=list[str])
    flags: dict[str, bool | int | str] = field(default_factory=dict[str, bool | int | str])
