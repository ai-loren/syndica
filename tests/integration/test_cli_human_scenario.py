"""Milestone-1 acceptance flows through declarative L2 replay and the CLI."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.kernel import ScriptedLLM, ScriptedTurn, ToolCall
from syndica.runner import ScenarioRunner, load_scenario

SCENARIOS = Path(__file__).parents[1] / 'scenarios'


async def test_l2_delegated_human_confirmation_scenario(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / 'delegated_human_confirmation.yaml')

    result = await ScenarioRunner().run(scenario, workspace_root=tmp_path)

    assert result.final_output == {
        'Coordinator[0]': 'workflow complete',
        'Worker[0]': 'worker complete',
    }
    assert result.workspace == {'result.txt': 'approved result'}
    assert [event.sequence for event in result.events] == list(range(1, len(result.events) + 1))


async def test_l2_rejected_confirmation_and_llm_failure_scenario(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(SCENARIOS / 'rejected_confirmation_failure.yaml')

    result = await ScenarioRunner().run(scenario, workspace_root=tmp_path)

    assert result.stop_reasons == {
        'Coordinator[0]': 'completed',
        'Worker[0]': 'error',
    }
    assert result.errors == {'Worker[0]': 'RuntimeError: injected LLM failure'}
    assert result.workspace == {'keep.txt': 'keep me'}
    assert any(event.kind == 'tool_confirmation_requested' for event in result.events)
    assert result.events[-1].kind == 'agent_end'


def test_cli_runs_a_real_delegated_human_confirmation_workflow(
    tmp_path: Path,
) -> None:
    config = tmp_path / '.syndica.yaml'
    config.write_text(
        """
version: 1
agents:
  - name: Worker
    description: CLI worker
    model: worker-model
    tools: [Write, Bash]
""",
        encoding='utf-8',
    )
    root_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='delegate-1',
                        name='Delegate',
                        arguments='{"agent":"Worker","query":"complete the workflow"}',
                    )
                ]
            ),
            ScriptedTurn(content='CLI workflow complete'),
        ]
    )
    worker_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='write-1',
                        name='Write',
                        arguments='{"path":"result.txt","content":"cli approved"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-1',
                        name='AskUser',
                        arguments='{"question":"Continue?","timeout":30}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='taint-1',
                        name='Bash',
                        arguments='{"command":"printf taint"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='dangerous-1',
                        name='Bash',
                        arguments='{"command":"rm -rf -- doomed.txt"}',
                    )
                ]
            ),
            ScriptedTurn(content='worker complete'),
        ]
    )
    doomed = tmp_path / 'doomed.txt'
    doomed.write_text('remove me', encoding='utf-8')
    stdout = StringIO()
    stderr = StringIO()
    io = CliIO(
        stdin=StringIO('continue\napprove\n'),
        stdout=stdout,
        stderr=stderr,
    )
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda name: worker_llm if name == 'worker-model' else root_llm,
        workspace_root=tmp_path,
    )
    try:
        code = main(
            [
                '--config',
                str(config),
                'run',
                'start',
                '--flag',
                'untrusted_high_risk=ask',
            ],
            host=host,
            io=io,
        )
    finally:
        host.close()

    [directory] = (tmp_path / '.syndica' / 'traces').iterdir()
    assert code == 0, stderr.getvalue()
    assert stderr.getvalue() == f'trace：.syndica/traces/{directory.name}\n'
    assert stdout.getvalue().endswith('CLI workflow complete\n')
    assert 'Continue?' in stdout.getvalue()
    assert 'dangerous-1' in stdout.getvalue()
    assert (tmp_path / 'result.txt').read_text(encoding='utf-8') == 'cli approved'
    assert doomed.exists() is False
