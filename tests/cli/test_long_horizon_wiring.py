"""CLI 接线：long-horizon 须与 acceptance 一起启用，经 host 真生效，trace show 看得到。"""

import json
import os
from io import StringIO
from pathlib import Path

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel.context import ToolCall
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn


def _cli(tmp_path: Path, llm: ScriptedLLM, argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(''), stdout=stdout, stderr=stderr)
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
    return code, stdout.getvalue(), stderr.getvalue()


def _config(tmp_path: Path, modules: str, flags: str = '') -> str:
    path = tmp_path / '.syndica.yaml'
    path.write_text(
        f'version: 1\nmodules: [{modules}]\n' + (f'flags:\n{flags}' if flags else ''),
        encoding='utf-8',
    )
    return str(path)


def _write(call_id: str, path: str) -> ScriptedTurn:
    arguments = {'path': path, 'content': 'notes'}
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name='Write', arguments=json.dumps(arguments))]
    )


def test_long_horizon_without_acceptance_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, 'long-horizon')
    code, _, stderr = _cli(
        tmp_path, ScriptedLLM([]), ['--config', config, 'run', 'go', '--agent', 'Worker']
    )
    assert code != 0
    assert 'long-horizon' in stderr and '加上 acceptance' in stderr


def test_a_drifting_run_restates_the_goal_and_trace_show_lists_it(tmp_path: Path) -> None:
    flags = (
        '  long_horizon_progress_every: 0\n'
        '  long_horizon_stall_turns: 0\n'
        '  long_horizon_drift_window: 2\n'
    )
    config = _config(tmp_path, 'acceptance, long-horizon', flags)
    llm = ScriptedLLM(
        [
            _write('w1', 'weather/paris.txt'),
            _write('w2', 'weather/rome.txt'),
            ScriptedTurn(content='done'),
        ]
    )
    goal = 'write the quarterly revenue report'
    argv = ['--config', config, 'run', 'go', '--agent', 'Worker', '--goal', goal]
    code, stdout, stderr = _cli(tmp_path, llm, argv)
    assert code == 0, stderr
    assert stdout == 'done\n'
    restated = [
        block
        for call in llm.calls
        for block in call
        if block.extra.get('syndica.kind') == 'strategy' and goal in str(block.content)
    ]
    assert restated

    [trace] = list((tmp_path / '.syndica' / 'traces').iterdir())
    code, shown, stderr = _cli(
        tmp_path, ScriptedLLM([]), ['--config', config, 'trace', 'show', trace.name]
    )
    assert code == 0, stderr
    section = shown.split('长程：\n', 1)[1].split('\n\n', 1)[0]
    assert 'goal.drift' in section and 'on_goal_drift → remind' in section
