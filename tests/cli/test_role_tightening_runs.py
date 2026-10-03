"""项目配置给角色的收紧（M4 D12）在每一种 run 里都落到 Agent 身上。拓扑 run 与 chat 不写回放
文档，收紧丢了没有任何记录会说出来；只读 run 里没有会执行命令的工具，处置只能从 Agent 自己身上
读。"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import syndica.cli.host as host_module
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    AgentFactory,
    BaseAgent,
    Disposition,
    EventBus,
    HooksCenter,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    ToolCall,
)
from syndica.kernel.danger import RuleTablePolicy

_RULE = 'shell.path_outside_workspace'
_OUTSIDE = 'cat /etc/hosts'

_CONFIG = f"""version: 1
agents:
  - name: Orchestrator
    description: 测试用的根，自己跑命令
    system_prompt: 按要求跑命令
    tools: [Bash]
  - name: Worker
    description: 改文件时用我
    tools: [Read]
danger:
  roles:
    Orchestrator: {{rules: {{{_RULE}: block}}}}
    Worker: {{rules: {{{_RULE}: block}}}}
topologies:
  solo:
    slots:
      - name: root
        agent: Orchestrator
        is_root: true
        cardinality: singleton
        boot: eager
"""


class _Recording(Runtime):
    policies: list[object] = []

    async def spawn(self, factory: AgentFactory, **kwargs: Any) -> str:
        def recording(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            agent = factory(hooks, events)
            _Recording.policies.append(agent.options.danger_policy)
            return agent

        return await super().spawn(recording, **kwargs)


@pytest.fixture
def policies(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    _Recording.policies = []
    monkeypatch.setattr(host_module, 'Runtime', _Recording)
    return _Recording.policies


def _outside_call() -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='outside',
                        name='Bash',
                        arguments=json.dumps({'command': _OUTSIDE}),
                    )
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )


def _host(tmp_path: Path, llm: ScriptedLLM) -> RuntimeCliHost:
    return RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path / 'ws',
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)},
    )


def _config(tmp_path: Path) -> str:
    (tmp_path / 'ws').mkdir(exist_ok=True)
    path = tmp_path / 'syndica.yaml'
    path.write_text(_CONFIG, encoding='utf-8')
    return str(path)


def _tool_output(llm: ScriptedLLM) -> str:
    [block] = [block for block in llm.calls[-1] if block.role == 'tool']
    return str(block.content)


def _enforced(policy: object) -> Disposition:
    assert isinstance(policy, RuleTablePolicy)
    return policy.table[_RULE]


def test_a_topology_run_blocks_what_the_project_config_tightened(
    tmp_path: Path, policies: list[object]
):
    config = _config(tmp_path)
    llm = _outside_call()
    host = _host(tmp_path, llm)
    try:
        host.run_request(RunRequest(prompt='go', config=config, topology='solo'))
    finally:
        host.close()

    assert '本次调用已被阻断' in _tool_output(llm)
    assert [_enforced(policy) for policy in policies] == [Disposition.BLOCK]


def test_a_chat_session_blocks_what_the_project_config_tightened(
    tmp_path: Path, policies: list[object]
):
    config = _config(tmp_path)
    llm = _outside_call()
    host = _host(tmp_path, llm)
    try:
        target = host.open_chat(config)
        host.run_request(RunRequest(prompt='go', session=target, config=config))
    finally:
        host.close()

    assert '本次调用已被阻断' in _tool_output(llm)
    assert [_enforced(policy) for policy in policies] == [Disposition.BLOCK]


def test_a_read_only_run_hands_the_tightening_to_the_agent_it_spawns(
    tmp_path: Path, policies: list[object]
):
    config = _config(tmp_path)
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    host = _host(tmp_path, llm)
    try:
        host.run_request(RunRequest(prompt='go', config=config, agent='Worker', no_write=True))
    finally:
        host.close()

    assert [_enforced(policy) for policy in policies] == [Disposition.BLOCK]
