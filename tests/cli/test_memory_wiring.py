"""CLI 接线 E2E（阶段五）：配置里启用 memory / compaction Module，经 host 真生效（§8.9 / §8.8）。"""

import os
from io import StringIO
from pathlib import Path

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel.context import ToolCall
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn


def _run(tmp_path: Path, llm: ScriptedLLM, argv: list[str]) -> tuple[int, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
        environ=dict(os.environ),
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close()
    return code, stderr.getvalue()


def test_enabling_memory_persists_a_remembered_fact_across_the_cli(tmp_path: Path) -> None:
    config = tmp_path / '.syndica.yaml'
    # 阈值 1：一条 Remember 立刻整理成长期记忆、落盘到 .syndica/memory。
    config.write_text(
        'version: 1\nmodules: [memory]\nflags:\n  memory_consolidate_after: 1\n',
        encoding='utf-8',
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='c1', name='Remember', arguments='{"text": "用 tab 缩进"}')
                ]
            ),
            # Remember 触发整理（阈值 1），整理用一次性调用——它排在这一轮工具执行期间，先于下一轮。
            ScriptedTurn(content='长期记忆：用 tab 缩进'),
            ScriptedTurn(content='done'),
        ]
    )
    code, stderr = _run(
        tmp_path, llm, ['--config', str(config), 'run', 'go', '--agent', 'Worker']
    )
    assert code == 0, stderr

    longs = list((tmp_path / '.syndica' / 'memory').glob('*/long.json'))
    assert longs, '记忆没有落盘：memory Module 没接进 host，或 Remember 工具没装上'
    assert '长期记忆：用 tab 缩进' in longs[0].read_text(encoding='utf-8')


def test_enabling_compaction_runs_without_error(tmp_path: Path) -> None:
    config = tmp_path / '.syndica.yaml'
    config.write_text(
        'version: 1\nmodules: [compaction]\n'
        'flags:\n  compaction_strategy: rule_based\n  compaction_context_tokens: 40\n',
        encoding='utf-8',
    )
    calls = [
        ScriptedTurn(tool_calls=[ToolCall(id=f'c{i}', name='Read', arguments='{"path": "a"}')])
        for i in range(3)
    ]
    llm = ScriptedLLM([*calls, ScriptedTurn(content='done')])
    code, stderr = _run(
        tmp_path, llm, ['--config', str(config), 'run', 'go', '--agent', 'Worker']
    )
    assert code == 0, stderr
