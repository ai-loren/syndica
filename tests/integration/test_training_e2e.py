"""M11 E2E：脚本化模型跑出 pass / partial / block / unverified 四种裁决，经 `syndica export` 导出。

run 走真的宿主与 Runtime（acceptance 模块、会话存盘、trace）。
导出走真的 `python -m syndica` 子进程。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel.context import ToolCall
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn

_GOAL = 'ship a report with a section four'
_SECRET = 'sk-train-e2e-quizvalue-0123456789'
_RUBRIC = {
    'id': 'R1',
    'statement': 'the report has a section four',
    'kind': 'checkable',
    'anchors': [{'source': 'user_message', 'locator': 'msg:goal', 'quote': 'section four'}],
}
_FLAGS = (
    '  acceptance_rubrics: true\n'
    '  acceptance_outcome: true\n'
    '  acceptance_outcome_block_retries: 0\n'
    '  acceptance_outcome_partial_retries: 0\n'
)


def _config(root: Path, *, acceptance: bool = True) -> str:
    name = '.syndica.yaml' if acceptance else 'plain.yaml'
    body = 'version: 1\nmodules: [acceptance]\nflags:\n' + _FLAGS if acceptance else 'version: 1\n'
    path = root / name
    path.write_text(body, encoding='utf-8')
    return str(path)


def _outcome(result: str, quote: str | None = None) -> ScriptedTurn:
    item: dict[str, object] = {'rubric_id': 'R1', 'result': result}
    if quote is not None:
        item['evidence'] = [{'source': 'user_message', 'locator': 'reply', 'quote': quote}]
    return ScriptedTurn(content=json.dumps({'outcomes': [item], 'reason': 'checked'}))


def _projection(rubrics: list[dict[str, object]]) -> ScriptedTurn:
    return ScriptedTurn(content=json.dumps({'rubrics': rubrics}))


def _run(root: Path, config: str, turns: list[ScriptedTurn], *, goal: bool = True) -> str:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: ScriptedLLM(turns),
        workspace_root=root,
        environ={**os.environ, 'TRAINING_PROBE_TOKEN': _SECRET},
    )
    argv = ['--config', config, 'run', 'write the report', '--agent', 'Worker']
    if goal:
        argv += ['--goal', _GOAL]
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close()
    assert code == 0, stderr.getvalue()
    return stderr.getvalue()


def _syndica(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_'))
    }
    return subprocess.run(
        [sys.executable, '-P', '-m', 'syndica', *args],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _fingerprint(root: Path) -> dict[str, str]:
    state = root / '.syndica'
    return {
        str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(state.rglob('*'))
        if path.is_file() and path.relative_to(state).parts[0] != 'exports'
    }


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


@pytest.fixture(scope='module')
def workspace(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    root = tmp_path_factory.mktemp('train-e2e')
    config = _config(root)
    sessions = root / '.syndica' / 'sessions'
    plan: list[tuple[str, list[ScriptedTurn]]] = [
        (
            'pass',
            [
                _projection([_RUBRIC]),
                ScriptedTurn(
                    tool_calls=[
                        ToolCall(
                            id='w1',
                            name='Write',
                            arguments=json.dumps(
                                {'path': 'report.md', 'content': '# Report\n\n## Section four\n'}
                            ),
                        )
                    ]
                ),
                ScriptedTurn(
                    content=f'Final answer: the report now has a section four. token {_SECRET}'
                ),
                _outcome('satisfied', 'the report now has'),
            ],
        ),
        (
            'partial',
            [
                _projection([_RUBRIC]),
                ScriptedTurn(content='Final answer: I started the report.'),
                _outcome('unconfirmed'),
            ],
        ),
        (
            'block',
            [
                _projection([_RUBRIC]),
                ScriptedTurn(content='Final answer: nothing was written.'),
                _outcome('unmet', 'nothing was written'),
            ],
        ),
        (
            'unverified',
            [
                _projection([]),
                ScriptedTurn(content='Final answer: done.'),
            ],
        ),
    ]
    by_verdict: dict[str, str] = {}
    for verdict, turns in plan:
        before = set(sessions.iterdir()) if sessions.is_dir() else set()
        stderr = _run(root, config, turns)
        assert f'验收：{verdict}' in stderr, stderr
        [created] = set(sessions.iterdir()) - before
        by_verdict[verdict] = created.name
    before = set(sessions.iterdir())
    _run(root, _config(root, acceptance=False), [ScriptedTurn(content='no goal here')], goal=False)
    [orphan] = set(sessions.iterdir()) - before
    by_verdict['orphan'] = orphan.name
    return root, by_verdict


def test_sft_export_keeps_order_tool_calls_and_artifacts_and_leaves_out_unverified(
    workspace: tuple[Path, dict[str, str]],
) -> None:
    root, sids = workspace
    before = _fingerprint(root)
    out = root / '.syndica' / 'exports' / 'sft'
    result = _syndica(root, 'export', 'sft', '--output', str(out))
    # 没有 Goal 的那个会话拼不出来：退出码 1，其余照样写出。
    assert result.returncode == 1, result.stderr
    rows = _lines(out / 'messages.jsonl')
    assert sorted(str(row['id']) for row in rows) == sorted(
        sids[name] for name in ('pass', 'partial', 'block')
    )
    passed = next(row for row in rows if row['id'] == sids['pass'])
    messages = passed['messages']
    assert isinstance(messages, list)
    # 用户的话、内核注入的 Goal / Rubric / 预算块，是模型当时看到的上下文，照实导出。
    assert [item['role'] for item in messages] == [
        'user',
        'user',
        'user',
        'user',
        'assistant',
        'tool',
        'assistant',
    ]
    assert messages[0]['content'] == 'write the report'
    assert _GOAL in str(messages[1]['content'])
    [call] = messages[4]['tool_calls']
    assert call['function']['name'] == 'Write'
    assert json.loads(call['function']['arguments'])['path'] == 'report.md'
    assert messages[5]['tool_call_id'] == call['id']
    assert str(messages[6]['content']).startswith('Final answer: the report now has')
    artifacts = passed['artifacts']
    assert isinstance(artifacts, list)
    assert [item['path'] for item in artifacts] == ['report.md']
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['include_unverified'] is False
    assert manifest['dropped_unverified'] == [sids['unverified']]
    assert any(str(item).startswith(sids['orphan']) for item in manifest['failed'])
    assert _fingerprint(root) == before


def test_no_exported_file_contains_the_raw_secret_and_placeholders_survive(
    workspace: tuple[Path, dict[str, str]],
) -> None:
    root, _ = workspace
    out = root / '.syndica' / 'exports' / 'all'
    for fmt in ('sft', 'sharegpt', 'preference', 'rl'):
        _syndica(root, 'export', fmt, '--output', str(out / fmt))
    texts = [path.read_text(encoding='utf-8') for path in out.rglob('*') if path.is_file()]
    assert texts
    assert all(_SECRET not in text for text in texts)
    sft = (out / 'sft' / 'messages.jsonl').read_text(encoding='utf-8')
    assert '[REDACTED:' in sft


def test_preference_pairs_trace_back_to_verdicts_of_the_same_goal_and_rubric(
    workspace: tuple[Path, dict[str, str]],
) -> None:
    root, sids = workspace
    out = root / '.syndica' / 'exports' / 'preference'
    _syndica(root, 'export', 'preference', '--output', str(out))
    [pair] = _lines(out / 'preference.jsonl')
    assert pair['chosen'] == {'id': sids['pass'], 'verdict': 'pass'}
    assert pair['rejected'] == {'id': sids['block'], 'verdict': 'block'}
    assert str(pair['goal_id']).startswith('goal:')
    assert str(pair['rubric_id']).startswith('rubric:')


def test_rl_rows_have_matching_masks_and_rewards_from_the_verdicts(
    workspace: tuple[Path, dict[str, str]],
) -> None:
    root, sids = workspace
    out = root / '.syndica' / 'exports' / 'rl'
    _syndica(root, 'export', 'rl', '--output', str(out))
    rows = _lines(out / 'rl.jsonl')
    rewards = {}
    for row in rows:
        ids, mask, episode = row['input_ids'], row['loss_mask'], row['episode']
        assert isinstance(ids, list) and isinstance(mask, list) and isinstance(episode, dict)
        assert len(ids) == len(mask)
        assert set(mask) == {0, 1}
        rewards[episode['id']] = row['reward']
    assert rewards == {sids['pass']: 1.0, sids['partial']: 0.5, sids['block']: 0.0}


def test_include_unverified_is_explicit_and_recorded(
    workspace: tuple[Path, dict[str, str]],
) -> None:
    root, sids = workspace
    out = root / '.syndica' / 'exports' / 'with-unverified'
    _syndica(root, 'export', 'sft', '--output', str(out), '--include-unverified')
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['include_unverified'] is True
    assert sids['unverified'] in manifest['sources']


def test_syndica_batch_runs_every_good_line_and_reports_the_bad_one(tmp_path: Path) -> None:
    fake = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[1] / 'e2e' / 'fake_openai.py')],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert fake.stdout is not None
        port = int(fake.stdout.readline().strip())
        root = tmp_path / 'ws'
        root.mkdir()
        (root / '.syndica.yaml').write_text('version: 1\n', encoding='utf-8')
        environ = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_'))
        }
        environ.update(
            {
                'XDG_CONFIG_HOME': str(tmp_path / 'config'),
                'SYNDICA_LLM_ORDER': 'openai',
                'OPENAI_BASE_URL': f'http://127.0.0.1:{port}/v1',
                'OPENAI_MODEL': 'fake',
                'OPENAI_API_KEY': 'e2e-dummy-key-0123456789',
            }
        )

        def syndica(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, '-P', '-m', 'syndica', *args],
                cwd=root,
                env=environ,
                capture_output=True,
                text=True,
                timeout=120,
            )

        probe = syndica('config', 'trust')
        digest = re.search(r'--sha256 ([0-9a-f]{64})', probe.stdout + probe.stderr)
        assert digest is not None, probe.stderr
        assert syndica('config', 'trust', '--sha256', digest.group(1)).returncode == 0
        dataset = root / 'tasks.jsonl'
        dataset.write_text(
            '{"id": "first", "prompt": "hello one"}\n'
            'not json\n'
            '{"id": "second", "prompt": "hello two"}\n',
            encoding='utf-8',
        )
        result = syndica('batch', str(dataset))
    finally:
        fake.terminate()
        fake.wait(timeout=5)
    assert result.returncode == 1, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[0]['line'] == 2 and rows[0]['ok'] is False
    assert rows[1:] == [{'id': 'first', 'ok': True}, {'id': 'second', 'ok': True}]
    assert len(list((root / '.syndica' / 'sessions').iterdir())) == 2
    assert 'e2e-dummy-key-0123456789' not in result.stdout + result.stderr


def test_trl_reads_the_exported_sft_file(workspace: tuple[Path, dict[str, str]]) -> None:
    pytest.importorskip('trl')
    from syndica.backends.trl.dry_run import dry_run

    root, _ = workspace
    out = root / '.syndica' / 'exports' / 'trl'
    _syndica(root, 'export', 'sft', '--output', str(out))
    result = dry_run(out / 'messages.jsonl')
    assert result['rows'] == 3
    prepared = result['prepared_columns']
    assert isinstance(prepared, list) and 'input_ids' in prepared
