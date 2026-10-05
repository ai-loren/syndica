"""CLI 接线 E2E（M7 阶段五）：配置里启用 acceptance，经 host 真生效（§7.4–§7.7）。"""

import asyncio
import json
import os
from io import StringIO
from pathlib import Path

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Goal,
    Rubric,
    RubricKind,
    RubricOrigin,
    RubricStatus,
)
from syndica.kernel.context import ToolCall
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.rubric_redaction import redacted_goal, redacted_rubric
from syndica.kernel.stores.file_rubric import FileRubricStore

_SID = 'a' * 32


def _cli(
    tmp_path: Path, llm: ScriptedLLM, argv: list[str], *, stdin: str = ''
) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
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


def _config(tmp_path: Path, flags: str = '') -> str:
    path = tmp_path / '.syndica.yaml'
    body = 'version: 1\nmodules: [acceptance]\n' + (f'flags:\n{flags}' if flags else '')
    path.write_text(body, encoding='utf-8')
    return str(path)


def _call(name: str, call_id: str, **arguments: object) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))]
    )


def _run(config: str, *extra: str) -> list[str]:
    return ['--config', config, 'run', 'write the report', '--agent', 'Worker', *extra]


def test_an_empty_deliverable_is_sent_back_through_the_cli(tmp_path: Path) -> None:
    # 宿主给 Runtime 接上产物索引，L1 才有登记可校验：空的交付物零 token 打回一次。
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='report.md', content=''),
            _call('Deliver', 'c2', path='report.md'),
            ScriptedTurn(content='done'),
            _call('Write', 'c3', path='report.md', content='# Report\n'),
            _call('Deliver', 'c4', path='report.md'),
            ScriptedTurn(content='done again'),
        ]
    )
    config = _config(tmp_path)
    code, stdout, stderr = _cli(tmp_path, llm, _run(config))
    assert code == 0, stderr
    assert stdout == 'done again\n'

    code, listed, stderr = _cli(
        tmp_path, ScriptedLLM([]), ['--config', config, 'artifacts', 'list']
    )
    assert code == 0, stderr
    assert listed.startswith('file:report.md\t交付物\tdocument\t') and '2 个版本' in listed
    show = ['--config', config, 'artifacts', 'show', 'file:report.md']
    code, shown, stderr = _cli(tmp_path, ScriptedLLM([]), show)
    assert code == 0, stderr
    assert [line.split('.')[0].strip() for line in shown.splitlines()[1:]] == ['1', '2']


def test_the_final_verdict_is_reported_on_stderr(tmp_path: Path) -> None:
    config = _config(tmp_path, '  acceptance_outcome: true\n')
    code, stdout, stderr = _cli(tmp_path, ScriptedLLM([ScriptedTurn(content='done')]), _run(config))
    assert code == 0, stderr
    assert stdout == 'done\n'
    assert '验收：unverified；没能验证（no_active_rubrics）' in stderr


def test_a_goal_without_the_acceptance_module_is_refused(tmp_path: Path) -> None:
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\n', encoding='utf-8')
    code, _, stderr = _cli(tmp_path, ScriptedLLM([]), _run(str(config), '--goal', 'ship it'))
    assert code != 0
    assert 'acceptance' in stderr and '--goal' in stderr


def test_an_acceptance_run_writes_no_replay_document(tmp_path: Path) -> None:
    config = _config(tmp_path, '  acceptance_outcome: true\n')
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    code, _, stderr = _cli(tmp_path, llm, _run(config, '--trace', 'replay'))
    assert code == 0, stderr
    traces = list((tmp_path / '.syndica' / 'traces').iterdir())
    assert traces and not any((trace / 'replay.json').exists() for trace in traces)


async def _seed(tmp_path: Path) -> FileRubricStore:
    store = FileRubricStore(tmp_path / '.syndica' / 'rubrics')
    anchor = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'column 3 is wrong'),)
    await store.put_goal(_SID, redacted_goal(Goal(intent='build the sales table'), RuleRedactor()))
    proposal = Rubric(
        id='fb:1',
        statement='column 3 uses confirmed revenue',
        kind=RubricKind.CHECKABLE,
        anchors=anchor,
        status=RubricStatus.PROPOSED,
        origin=RubricOrigin.FEEDBACK,
    )
    await store.propose(_SID, redacted_rubric(proposal, RuleRedactor()))
    return store


def test_sessions_rubrics_lists_a_proposal_and_activate_makes_it_count(tmp_path: Path) -> None:
    store = asyncio.run(_seed(tmp_path))
    config = _config(tmp_path)
    code, stdout, stderr = _cli(
        tmp_path, ScriptedLLM([]), ['--config', config, 'sessions', 'rubrics', _SID]
    )
    assert code == 0, stderr
    assert 'build the sales table' in stdout and 'fb:1  proposed' in stdout
    assert f'syndica sessions activate {_SID}' in stdout

    code, stdout, stderr = _cli(
        tmp_path, ScriptedLLM([]), ['--config', config, 'sessions', 'activate', _SID, 'fb:1']
    )
    assert code == 0, stderr
    assert [r.id for r in asyncio.run(store.rubrics(_SID))] == ['fb:1']

    again = ['--config', config, 'sessions', 'activate', _SID, 'fb:1']
    code, _, stderr = _cli(tmp_path, ScriptedLLM([]), again)
    assert code != 0 and '已经生效' in stderr  # 不把一次无操作报成「已生效」


def test_activating_something_that_is_not_a_proposal_says_where_to_look(tmp_path: Path) -> None:
    asyncio.run(_seed(tmp_path))
    config = _config(tmp_path)
    code, _, stderr = _cli(
        tmp_path, ScriptedLLM([]), ['--config', config, 'sessions', 'activate', _SID, 'nope']
    )
    assert code != 0
    assert 'nope' in stderr and 'sessions rubrics' in stderr


def test_chat_tells_the_human_about_a_feedback_proposal(tmp_path: Path) -> None:
    # 提议不会自己生效：人不知道它在，它就永远停在提议。
    config = _config(tmp_path, '  acceptance_feedback: true\n')
    proposer = ScriptedTurn(
        content=json.dumps({'statement': 'column 3 uses confirmed revenue', 'kind': 'checkable'})
    )
    llm = ScriptedLLM([ScriptedTurn(content='v1'), proposer, ScriptedTurn(content='v2')])
    code, _, stderr = _cli(
        tmp_path,
        llm,
        ['--config', config, 'chat'],
        stdin='build the sales table\ncolumn 3 is wrong, use confirmed revenue\n',
    )
    assert code == 0, stderr
    assert '提议的验收标准 fb:' in stderr
    assert 'syndica sessions activate' in stderr


def test_artifacts_without_an_index_says_how_to_get_one(tmp_path: Path) -> None:
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\n', encoding='utf-8')
    argv = ['--config', str(config), 'artifacts', 'list']
    code, _, stderr = _cli(tmp_path, ScriptedLLM([]), argv)
    assert code != 0 and 'acceptance' in stderr


def test_showing_an_unknown_artifact_points_at_the_list(tmp_path: Path) -> None:
    (tmp_path / '.syndica' / 'artifacts').mkdir(parents=True)
    argv = ['--config', _config(tmp_path), 'artifacts', 'show', 'file:nope.md']
    code, _, stderr = _cli(tmp_path, ScriptedLLM([]), argv)
    assert code != 0 and 'artifacts list' in stderr
