"""托管的 ACP Agent 是真子进程：它的环境是 Agent 环境加上用户级配置点名的宿主变量，别无其他。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from syndica.backends.acp.model import AcpAgentModel
from syndica.cli.environment import agent_environment
from syndica.kernel import (
    BlockHighRisk,
    ContextBlock,
    DangerJudge,
    ExternalCallPolicy,
    Runtime,
)
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.workspace_router import WorkspaceRouter

# 第一行就记下环境：之后 import 的任何东西都不会出现在记录里。
_AGENT = """
import os, sys
seen = dict(os.environ)

import asyncio, json


async def main():
    from acp.agent.connection import AgentSideConnection
    from acp.schema import Implementation, InitializeResponse, NewSessionResponse, PromptResponse
    from acp.stdio import stdio_streams

    class Reports:
        async def initialize(self, protocol_version, **kwargs):
            return InitializeResponse(
                protocol_version=1, agent_info=Implementation(name='env', version='0')
            )

        async def new_session(self, cwd, **kwargs):
            return NewSessionResponse(session_id='env')

        async def prompt(self, session_id, prompt, **kwargs):
            with open(sys.argv[1] + '.tmp', 'w') as handle:
                json.dump(seen, handle)
            os.replace(sys.argv[1] + '.tmp', sys.argv[1])
            return PromptResponse(stop_reason='end_turn')

        async def cancel(self, session_id, **kwargs):
            pass

    reader, writer = await stdio_streams()
    AgentSideConnection(Reports(), writer, reader)
    await asyncio.Event().wait()


asyncio.run(main())
"""

# macOS 在 exec 时给每个进程补 __CF_USER_TEXT_ENCODING；环境里没有 locale 时 Python 自己补 LC_CTYPE
# （PEP 538）。两者都不是从父进程来的。
_ADDED_BY_THE_PLATFORM = frozenset({'__CF_USER_TEXT_ENCODING', 'LC_CTYPE'})


async def test_the_hosted_process_sees_the_agent_environment_and_only_the_named_host_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('SYNDICA_PROBE_HOST_ONLY_TOKEN', 'quiz-host-process-only')
    root = tmp_path / 'ws'
    root.mkdir()
    script = tmp_path / 'env_agent.py'
    script.write_text(_AGENT, encoding='utf-8')
    report = tmp_path / 'seen.json'
    secrets = {
        'GEMINI_API_KEY': 'quiz-named-value',
        'AWS_SECRET_ACCESS_KEY': 'quiz-unnamed-value',
        'GITHUB_TOKEN': 'quiz-unnamed-token',
    }
    host = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path), **secrets}
    agent_env = agent_environment(host, (), workspace_root=root)
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env=dict(agent_env.values)),
        taint=FileTaint(root),
    )
    runtime = Runtime(workspace=router)
    model = AcpAgentModel(
        'gemini',
        command=(sys.executable, str(script), str(report)),
        pass_env=('GEMINI_API_KEY',),
    )
    try:
        model.bind_runtime(
            runtime,
            {},
            ExternalCallPolicy(judge=DangerJudge(), policy=BlockHighRisk()),
            role='Worker',
            agent_env=agent_env.values,
            host_env=host,
            redactor=RuleRedactor(secrets=list(secrets.values())),
        )
        await asyncio.wait_for(model.create([ContextBlock(role='user', content='go')]), 30)
    finally:
        await model.aclose()
        await runtime.shutdown()
        await router.close()
        agent_env.remove()

    seen = json.loads(report.read_text(encoding='utf-8'))
    expected = {**agent_env.values, 'GEMINI_API_KEY': 'quiz-named-value'}
    assert not _ADDED_BY_THE_PLATFORM & set(expected)
    assert {name: value for name, value in seen.items() if name not in _ADDED_BY_THE_PLATFORM} == (
        expected
    )
    assert 'AWS_SECRET_ACCESS_KEY' not in seen
    assert 'GITHUB_TOKEN' not in seen
    assert 'SYNDICA_PROBE_HOST_ONLY_TOKEN' not in seen
    assert seen['SYNDICA_AGENT_ENV'] == '1'
