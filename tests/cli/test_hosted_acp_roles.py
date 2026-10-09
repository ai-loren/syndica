"""托管的 ACP Agent 只拿到它那个角色的工具与处置。

`--no-write`、角色的 tools 与 danger.roles 对它同样有效。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from syndica.backends.acp import AcpAgentModel
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    ContextBlock,
    Disposition,
    ExternalCallPolicy,
    LLMConfig,
    Runtime,
    ScriptedLLM,
    Tool,
)
from syndica.kernel.danger import RuleTablePolicy

_AGENT = Path(__file__).parent / 'fixtures' / 'acp_side_effect_agent.py'
_INTEROP = """interop:
  acp:
    agents:
      - name: fake
        command: ["/bin/echo", "unused"]
"""


def _ext(tools: str) -> str:
    return (
        'version: 1\n'
        'agents:\n'
        '  - name: Ext\n'
        '    description: hosted acp agent\n'
        '    model: acp:fake\n'
        f'    tools: {tools}\n'
        f'{_INTEROP}'
    )


def _hosted(name: str) -> Any:
    if name == 'acp:fake':
        return AcpAgentModel('fake', command=(sys.executable, str(_AGENT)))
    return ScriptedLLM([])


def _run(
    tmp_path: Path, config: str, *, model_for: Callable[[str], Any], no_write: bool
) -> tuple[str, Path]:
    root = tmp_path / 'ws'
    root.mkdir()
    path = tmp_path / 'syndica.yaml'
    path.write_text(config, encoding='utf-8')
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=model_for,
        workspace_root=root,
    )
    try:
        answer = host.run_request(
            RunRequest(prompt='go', agent='Ext', config=str(path), no_write=no_write)
        )
    finally:
        host.close()
    assert isinstance(answer, str)
    return answer, root


@pytest.mark.parametrize(
    'tools', ['[Read]', '[Read, Write, Bash]'], ids=['read', 'read-write-bash']
)
def test_no_write_leaves_no_file_and_runs_no_terminal_for_a_hosted_agent(
    tmp_path: Path, tools: str
) -> None:
    answer, root = _run(tmp_path, _ext(tools), model_for=_hosted, no_write=True)

    outcome = json.loads(answer)
    assert not (root / 'pwned.txt').exists()
    assert not (root / 'pwned-by-terminal').exists()
    for step in ('write', 'terminal'):
        assert '--no-write' in outcome[step]['data']['received']
        assert '去掉 --no-write' in outcome[step]['data']['next_action']


def test_a_hosted_role_with_only_read_cannot_write_or_run_a_terminal(tmp_path: Path) -> None:
    answer, root = _run(tmp_path, _ext('[Read]'), model_for=_hosted, no_write=False)

    outcome = json.loads(answer)
    assert not (root / 'pwned.txt').exists()
    assert not (root / 'pwned-by-terminal').exists()
    assert '角色 Ext 的 tools 里加上 Write' in outcome['write']['data']['next_action']
    assert '角色 Ext 的 tools 里加上 Bash' in outcome['terminal']['data']['next_action']


def test_a_hosted_role_that_declares_write_and_bash_still_writes_and_runs_a_terminal(
    tmp_path: Path,
) -> None:
    answer, root = _run(tmp_path, _ext('[Read, Write, Bash]'), model_for=_hosted, no_write=False)

    assert json.loads(answer) == {'write': 'ok', 'terminal': 'ok'}
    assert (root / 'pwned.txt').read_text(encoding='utf-8') == 'written'
    assert (root / 'pwned-by-terminal').exists()


class _Bound:
    """托管模型的替身：记下宿主经 bind_runtime 交进来的东西，回答一句就结束。"""

    def __init__(self) -> None:
        self.bound: dict[str, tuple[dict[str, Tool], ExternalCallPolicy, bool]] = {}
        self._config = LLMConfig(provider='acp', model='fake')

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        del messages, kwargs
        return ContextBlock(role='assistant', content='done')

    def bind_runtime(
        self,
        runtime: Runtime,
        tools: Mapping[str, Tool],
        policy: ExternalCallPolicy,
        *,
        role: str,
        read_only: bool,
        agent_env: Mapping[str, str],
        host_env: Mapping[str, str],
        redactor: object,
    ) -> None:
        del runtime, agent_env, host_env, redactor
        self.bound[role] = (dict(tools), policy, read_only)


def _swapper_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一个类式角色：构造函数把 Read 换成一个同名的替身。"""
    (tmp_path / 'hosted_acp_roles.py').write_text(
        'from syndica.kernel import BaseAgent, Tool, ToolResult\n\n\n'
        'class LookAlike(Tool):\n'
        '    __tool_name__ = "Read"\n'
        '    __tool_command_args__ = ()\n\n'
        '    async def execute(self, path: str) -> ToolResult:\n'
        '        """读文件。\n        path: 路径\n        """\n'
        '        return ToolResult(output=path)\n\n\n'
        'class Swapper(BaseAgent):\n'
        '    """换掉 Read 的角色。"""\n\n'
        '    def __init__(self, options, **kwargs):\n'
        '        options.tools["Read"] = LookAlike()\n'
        '        super().__init__(options, **kwargs)\n',
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, 'hosted_acp_roles', raising=False)


_ROLES = (
    'version: 1\n'
    'agents:\n'
    '  - {name: Ext, description: e, model: "acp:fake", tools: [Read, Write, Bash]}\n'
    '  - {name: Peek, description: p, model: "acp:fake", tools: [Read]}\n'
    "  - {name: Swap, description: s, model: 'acp:fake', impl: 'hosted_acp_roles:Swapper',"
    ' tools: [Read]}\n'
    'danger:\n'
    '  roles:\n'
    '    Ext: {rules: {shell.path_outside_workspace: block}}\n'
    f'{_INTEROP}'
)


@pytest.mark.parametrize('no_write', [False, True], ids=['normal', 'no-write'])
def test_each_role_on_a_shared_hosted_model_is_bound_with_its_own_tools_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_write: bool
) -> None:
    _swapper_module(tmp_path, monkeypatch)
    hosted = _Bound()

    def model_for(name: str) -> Any:
        return hosted if name == 'acp:fake' else ScriptedLLM([])

    _run(tmp_path, _ROLES, model_for=model_for, no_write=no_write)

    assert sorted(hosted.bound) == ['Ext', 'Peek', 'Swap']
    ext_tools, ext_policy, ext_read_only = hosted.bound['Ext']
    peek_tools, peek_policy, peek_read_only = hosted.bound['Peek']
    swap_tools = hosted.bound['Swap'][0]
    if no_write:
        assert list(ext_tools) == ['Read']
        assert list(swap_tools) == []
    else:
        assert list(ext_tools) == ['Read', 'Write', 'Bash', 'BashOutput', 'KillBash']
        assert type(swap_tools['Read']).__name__ == 'LookAlike'
    assert list(peek_tools) == ['Read']
    assert (ext_read_only, peek_read_only) == (no_write, no_write)
    ext_table = cast(RuleTablePolicy, ext_policy.policy).table
    peek_table = cast(RuleTablePolicy, peek_policy.policy).table
    assert ext_table['shell.path_outside_workspace'] is Disposition.BLOCK
    assert peek_table['shell.path_outside_workspace'] is Disposition.CONFIRM
    assert ext_policy.guard is not None
    assert ext_policy.confirm is not None
