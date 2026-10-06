"""后台调用的两个工具（spec §5.6）：查一个后台调用还在不在跑、停掉一个不再需要的。

只读与取消都只碰这个 Agent 自己的后台调用表：编号是它自己那一张表里的，别的 Agent 的编号在这里查不
到。
"""

from __future__ import annotations

from syndica.kernel.background import BackgroundCalls
from syndica.kernel.tool import Tool, ToolResult

__all__ = ['CancelBackgroundTool', 'QueryBackgroundTool']


def _unknown(background_id: str) -> ToolResult:
    return ToolResult(
        output=(
            f'期望: {background_id!r} 是你转到后台的某次调用的编号（形如 bg-1）。'
            ' 实际: 你的后台调用里没有这个编号。'
            ' 下一步: 用转入后台时那条结果里给出的编号；不确定就当它已经结束，接着做别的。'
        ),
        meta={'background_id': background_id, 'state': None},
    )


class QueryBackgroundTool(Tool):
    __tool_name__ = 'QueryBackgroundTool'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(self, calls: BackgroundCalls) -> None:
        self._calls = calls

    async def execute(self, background_id: str) -> ToolResult:
        """查一次转到后台的调用：还在跑、已经出结果，还是被取消了。只看不碰。
        结果出来之后会自己进你的历史，不必反复来查。
        background_id: 转入后台时给出的编号，形如 bg-1
        """
        entry = self._calls.get(background_id)
        if entry is None:
            return _unknown(background_id)
        if entry.state == 'running':
            text = (
                f'{entry.id}（{entry.call.name}）还在跑，'
                f'已经跑了 {self._calls.elapsed(entry):.0f} 秒；结果出来后会自己进你的历史。'
            )
        elif entry.state == 'done':
            text = f'{entry.id}（{entry.call.name}）已经结束，结果在你的历史里（或下一轮进来）。'
        else:
            text = f'{entry.id}（{entry.call.name}）已被取消（{entry.why}）。'
        return ToolResult(output=text, meta={'background_id': entry.id, 'state': entry.state})


class CancelBackgroundTool(Tool):
    __tool_name__ = 'CancelBackgroundTool'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(self, calls: BackgroundCalls) -> None:
        self._calls = calls

    async def execute(self, background_id: str) -> ToolResult:
        """停掉一次还在后台跑的调用；它已经做了的那部分不会被撤销。
        不再需要它的结果时用它，免得交付之前还要等它。
        background_id: 转入后台时给出的编号，形如 bg-1
        """
        entry = self._calls.get(background_id)
        if entry is None:
            return _unknown(background_id)
        cancelled = self._calls.cancel(background_id, '你自己取消的', report=False)
        if cancelled is None:
            return ToolResult(
                output=f'{entry.id}（{entry.call.name}）已经不在跑了（{entry.state}），不必再取消。',
                meta={'background_id': entry.id, 'state': entry.state},
            )
        return ToolResult(
            output=(
                f'{entry.id}（{entry.call.name}）已取消。它在被取消之前可能已经做了一部分，'
                '那部分不会被撤销；需要的话自己核对一下。'
            ),
            meta={'background_id': entry.id, 'state': 'cancelled'},
        )
