"""中间件契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ['HookResult']


@dataclass
class HookResult:
    """一个 hook 对本次操作的意见。

    `skip` 跳过原操作并终止后续可拦截 hook；`modified` 只写要改的键，会并入后续 hook 与原
    操作的参数；`result` 配合 `skip=True` 给出替代结果（如 pre_tool_use 直接给出工具输出）。

    只有可拦截切点读它：可观察切点上返回的 `HookResult` 被整个丢弃。
    """

    skip: bool = False
    modified: dict[str, Any] = field(default_factory=dict[str, Any])
    result: Any = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.modified.get(key, default)
