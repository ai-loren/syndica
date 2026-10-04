"""中间件契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ['HookResult']


@dataclass
class HookResult:
    """一个 hook 对本次操作的意见。

    `skip` 跳过原操作并终止后续可拦截 hook；`modified` 只写要改的键，会并入后续 hook 与原
    操作的参数；`result` **只在 `skip=True` 时**给出替代结果（如 pre_tool_use 直接给出工具输出）
    ——不带 `skip` 的 `result` 哪个切点都读不到，`HooksCenter` 当场报 ContractViolation。

    `history` 只出现在 `HooksCenter.trigger` 的返回值上（hook 自己填它会被忽略）：`modified`
    是 last-wins 的结论，而 `history[key]` 是这个键在链上被依次赋过的**全部**值。只能单向收紧的
    量必须看后者——链中间一个 hook 把 trust 收紧、后一个 hook 交出带默认 trust 的新值时，结论里
    那次收紧已经不见了（spec §12.2）。

    `untrusted` 同样只出现在 `trigger` 的返回值上：这份结论里有被收紧的 Module 写下的改写或跳过，
    拿它的值去做事的代码要按不可信对待（结论本身不带作者）。

    只有可拦截切点读它：可观察切点上返回的 `HookResult` 被整个丢弃。字段顺序计入公开 API。
    """

    skip: bool = False
    modified: dict[str, Any] = field(default_factory=dict[str, Any])
    result: Any = None
    history: dict[str, list[Any]] = field(default_factory=dict[str, list[Any]])
    untrusted: bool = False

    def get(self, key: str, default: Any = None) -> Any:
        return self.modified.get(key, default)

    def rewrites(self, key: str) -> list[Any]:
        """`key` 在链上被赋过的每一个值，按赋值顺序；没被改过则空列表。"""
        return list(self.history.get(key, []))
