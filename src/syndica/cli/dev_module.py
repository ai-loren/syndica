"""开发期 Module（spec D4）：git 写命令与删根在 PRE_TOOL_USE 上无条件拦下。

只靠处置表挡不住：读过命令输出之后，高风险调用按 `untrusted_high_risk` 处置、不再问宿主策略，`ask`
时会问人、`allow` 时会放行。所以这两族在更早的切点上拦，与那个 Flag 取什么值无关。

拦截交回的 `ToolResult` 标 `TRUSTED`：这段文字出自宿主，不是命令输出。标成不可信会把整条轨迹变成
不可信，此后每一次越界确认都直接变成拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from syndica.cli.policy import dev_guard_judge
from syndica.kernel import (
    DangerJudge,
    DangerVerdict,
    HookResult,
    HooksCenter,
    Junction,
    RiskLevel,
    RuntimeModule,
    Tool,
    ToolResult,
    TrustLevel,
)

__all__ = ['DevModule']

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


class DevModule(RuntimeModule):
    """每个 Agent 一个拦截插件。`workspace_root` 在构造时解析一次，规则不读 cwd。"""

    def __init__(self, workspace_root: Path) -> None:
        judge = dev_guard_judge(workspace_root)

        def guard(runtime: object, agent: object, flags: object) -> _Guard:
            del runtime, agent, flags
            return _Guard(judge)

        super().__init__('dev', agent_plugins=(guard,))
