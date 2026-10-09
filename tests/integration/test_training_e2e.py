"""M11 E2E：一份任务清单经组合根的 `batch` 跑出 pass / partial / block / unverified，
经 `syndica export` 导出。

run 走真的宿主与 Runtime（acceptance 模块、会话存盘、trace）。导出走真的 `python -m syndica`
子进程，用导出那一刻的宿主环境与 `.syndica.yaml` 的用户规则脱敏。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from syndica.__main__ import _Training  # pyright: ignore[reportPrivateUsage]
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, CliOutcome, RunRequest, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel.context import ToolCall
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import RedactionRule, RuleRedactor

_GOAL = 'ship a report with a section four'
# 宿主环境里有它，run 时就被换掉；它代表「一个本来就会被宿主认出的密钥」。
_RUN_SECRET = 'sk-train-e2e-quizvalue-0123456789'
# run 时宿主环境里没有它，会话里存的是原文；只有导出时的环境里有。
_EXPORT_SECRET = 'zq-export-only-credential-quiz-4821'
# run 之后才加进 `.syndica.yaml` 的用户规则要认的内网主机名。
_HOST = 'corp-db1.internal'
_RULE = RedactionRule('custom.internal_host', r'corp-db1\.internal')
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


def _write_config(root: Path, *, rules: bool = False) -> str:
    body = 'version: 1\nmodules: [acceptance]\nflags:\n' + _FLAGS
    if rules:
        body += f"redaction:\n  rules:\n    - {{id: {_RULE.rule_id}, pattern: '{_RULE.pattern}'}}\n"
    path = root / '.syndica.yaml'
    path.write_text(body, encoding='utf-8')
    return str(path)


def _outcome(result: str, quote: str | None = None) -> ScriptedTurn:
    item: dict[str, object] = {'rubric_id': 'R1', 'result': result}
    if quote is not None:
        item['evidence'] = [{'source': 'user_message', 'locator': 'reply', 'quote': quote}]
    return ScriptedTurn(content=json.dumps({'outcomes': [item], 'reason': 'checked'}))


def _projection(rubrics: list[dict[str, object]]) -> ScriptedTurn:
    return ScriptedTurn(content=json.dumps({'rubrics': rubrics}))


def _host(root: Path, llm: ScriptedLLM, stderr: StringIO) -> RuntimeCliHost:
    return RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda _: llm,
        workspace_root=root,
        environ={**os.environ, 'TRAINING_PROBE_TOKEN': _RUN_SECRET},
    )


class _Lazy:
    """`_Training` 要的那两个方法；宿主是测试建的、模型是脚本化的。"""

    def __init__(self, host: RuntimeCliHost) -> None:
        self._host = host

    def run_request(self, request: RunRequest) -> str | CliOutcome:
        return self._host.run_request(request)

    def runtime_host(self) -> RuntimeCliHost:
        return self._host


def _environ(config_home: Path) -> dict[str, str]:
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_'))
    }
    environ['XDG_CONFIG_HOME'] = str(config_home)
    environ['EXPORT_ONLY_API_KEY'] = _EXPORT_SECRET
    return environ


def _syndica(root: Path, environ: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-P', '-m', 'syndica', *args],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _trust(root: Path, environ: dict[str, str]) -> None:
    probe = _syndica(root, environ, 'config', 'trust')
    digest = re.search(r'--sha256 ([0-9a-f]{64})', probe.stdout + probe.stderr)
    assert digest is not None, probe.stdout + probe.stderr
    trusted = _syndica(root, environ, 'config', 'trust', '--sha256', digest.group(1))
    assert trusted.returncode == 0, trusted.stderr


def _fingerprint(root: Path) -> dict[str, str]:
    state = root / '.syndica'
    return {
        str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(state.rglob('*'))
        if path.is_file()
    }


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _sessions_by_creation(root: Path) -> list[str]:
    metas = [
        json.loads((path / 'meta.json').read_text(encoding='utf-8'))['meta']
        for path in (root / '.syndica' / 'sessions').iterdir()
    ]
    return [meta['sid'] for meta in sorted(metas, key=lambda meta: meta['created_at'])]


class _Workspace:
    def __init__(self, root: Path, sids: dict[str, str], environ: dict[str, str], out: Path):
        self.root = root
        self.sids = sids
        self.environ = environ
        self.out = out

    def export(self, fmt: str, name: str, *extra: str) -> tuple[Path, int, str]:
        target = self.out / name
        result = _syndica(self.root, self.environ, 'export', fmt, '--output', str(target), *extra)
        return target, result.returncode, result.stdout + result.stderr


@pytest.fixture(scope='module')
def workspace(tmp_path_factory: pytest.TempPathFactory) -> _Workspace:
    root = tmp_path_factory.mktemp('train-e2e')
    config = _write_config(root)
    tasks = [
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
                    content=(
                        'Final answer: the report now has a section four. '
                        f'token {_RUN_SECRET} export {_EXPORT_SECRET} host {_HOST}'
                    )
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
        ('unverified', [_projection([]), ScriptedTurn(content='Final answer: done.')]),
    ]
    turns = [turn for _, script in tasks for turn in script]
    dataset = root / 'tasks.jsonl'
    dataset.write_text(
        ''.join(
            json.dumps({'id': name, 'prompt': 'write the report', 'agent': 'Worker', 'goal': _GOAL})
            + '\n'
            for name, _ in tasks
        ),
        encoding='utf-8',
    )
    stderr = StringIO()
    host = _host(root, ScriptedLLM(turns), stderr)
    try:
        outcome = _Training(_Lazy(host)).batch(str(dataset), config)  # pyright: ignore[reportArgumentType]
    finally:
        host.close()
    assert outcome.exit_code == 0, outcome.output + stderr.getvalue()
    reported = re.findall(r'验收：(pass|partial|block|unverified)', stderr.getvalue())
    assert reported == ['pass', 'partial', 'block', 'unverified'], stderr.getvalue()
    sids = dict(zip([name for name, _ in tasks], _sessions_by_creation(root), strict=True))

    # 打回一次再过：同一个会话里根 Agent 先被判 partial，再被判 pass。
    retry = ScriptedLLM(
        [
            _projection([_RUBRIC]),
            ScriptedTurn(content='Final answer: I started the report.'),
            _outcome('unconfirmed'),
            ScriptedTurn(content='Final answer: the report now has a section four.'),
            _outcome('satisfied', 'the report now has'),
        ]
    )
    before = set(_sessions_by_creation(root))
    retry_err = StringIO()
    retry_host = _host(root, retry, retry_err)
    argv = [
        '--config',
        config,
        'run',
        'write the report',
        '--agent',
        'Worker',
        '--goal',
        _GOAL,
        '--flag',
        'acceptance_outcome_partial_retries=1',
    ]
    try:
        assert main(argv, host=retry_host, io=CliIO(StringIO(), StringIO(), retry_err)) == 0
    finally:
        retry_host.close()
    [sids['retry']] = set(_sessions_by_creation(root)) - before

    plain = root / 'plain.yaml'
    plain.write_text('version: 1\n', encoding='utf-8')
    before = set(_sessions_by_creation(root))
    orphan_host = _host(root, ScriptedLLM([ScriptedTurn(content='no goal here')]), StringIO())
    try:
        argv = ['--config', str(plain), 'run', 'write the report', '--agent', 'Worker']
        assert main(argv, host=orphan_host, io=CliIO(StringIO(), StringIO(), StringIO())) == 0
    finally:
        orphan_host.close()
    [sids['orphan']] = set(_sessions_by_creation(root)) - before

    # 规则在 run 之后才加：导出必须读此刻的配置，而不是只靠 run 时已经脱敏过的文本。
    _write_config(root, rules=True)
    environ = _environ(tmp_path_factory.mktemp('train-config'))
    _trust(root, environ)
    return _Workspace(root, sids, environ, tmp_path_factory.mktemp('train-exports'))


def test_the_session_still_holds_what_only_export_time_redaction_can_catch(
    workspace: _Workspace,
) -> None:
    record = (
        workspace.root / '.syndica' / 'sessions' / workspace.sids['pass'] / 'record.json'
    ).read_text(encoding='utf-8')
    assert _EXPORT_SECRET in record and _HOST in record
    assert _RUN_SECRET not in record


def test_sft_export_keeps_order_tool_calls_and_artifacts_and_leaves_out_unverified(
    workspace: _Workspace,
) -> None:
    sids = workspace.sids
    before = _fingerprint(workspace.root)
    out, code, said = workspace.export('sft', 'sft')
    # 没有 Goal 的那个会话拼不出来：退出码 1，其余照样写出。
    assert code == 1, said
    assert '会话没拼成' in said
    rows = _lines(out / 'messages.jsonl')
    assert sorted(str(row['id']) for row in rows) == sorted(
        sids[name] for name in ('pass', 'partial', 'block', 'retry')
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
    assert all(item['version'] for item in artifacts)
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['include_unverified'] is False
    assert manifest['dropped_unverified'] == [sids['unverified']]
    assert any(str(item).startswith(sids['orphan']) for item in manifest['failed'])
    samples = {sample['id']: sample for sample in manifest['samples']}
    assert samples[sids['retry']]['verdicts'] == ['partial', 'pass']
    assert samples[sids['pass']]['verdict_source'] == 'acceptance'
    assert samples[sids['pass']]['trace_id']
    assert _fingerprint(workspace.root) == before


def test_export_uses_the_hosts_rules_and_secrets_of_this_moment(workspace: _Workspace) -> None:
    texts: list[str] = []
    for fmt in ('sft', 'sharegpt', 'preference', 'rl', 'trajectory'):
        out, _, _ = workspace.export(fmt, f'all-{fmt}')
        texts += [path.read_text(encoding='utf-8') for path in out.iterdir()]
    assert texts
    for secret in (_RUN_SECRET, _EXPORT_SECRET, _HOST):
        assert all(secret not in text for text in texts), secret
    sft = next(text for text in texts if '"messages"' in text and '"untrusted"' in text)
    assert '[REDACTED:secret.env_value]' in sft
    assert f'[REDACTED:{_RULE.rule_id}]' in sft
    manifest = json.loads((workspace.out / 'all-sft' / 'manifest.json').read_text())
    assert manifest['redaction']['digest'] == RuleRedactor(rules=[_RULE]).digest


def test_preference_pairs_trace_back_to_verdicts_of_the_same_goal_and_rubric(
    workspace: _Workspace,
) -> None:
    sids = workspace.sids
    out, _, _ = workspace.export('preference', 'preference')
    [pair] = _lines(out / 'preference.jsonl')
    assert pair['chosen_verdict'] == 'pass'
    assert pair['chosen_id'] in {sids['pass'], sids['retry']}
    assert (pair['rejected_id'], pair['rejected_verdict']) == (sids['block'], 'block')
    assert str(pair['goal_id']).startswith('goal:')
    assert str(pair['rubric_id']).startswith('rubric:')
    assert isinstance(pair['prompt'], list) and pair['prompt'][0]['role'] == 'user'


def test_rl_rows_mask_the_assistant_and_reward_the_verdict(workspace: _Workspace) -> None:
    sids = workspace.sids
    out, _, _ = workspace.export('rl', 'rl')
    rows = _lines(out / 'rl.jsonl')
    rewards = {}
    for row in rows:
        ids, mask, episode = row['input_ids'], row['loss_mask'], row['episode']
        assert isinstance(ids, list) and isinstance(mask, list) and isinstance(episode, dict)
        assert len(ids) == len(mask)
        assert set(mask) == {0, 1}
        rewards[episode['id']] = row['reward']
    assert rewards == {
        sids['pass']: 1.0,
        sids['retry']: 1.0,
        sids['partial']: 0.5,
        sids['block']: 0.0,
    }
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['tokenizer'] == 'utf8-bytes'


def test_include_unverified_is_explicit_and_recorded(workspace: _Workspace) -> None:
    out, _, _ = workspace.export('sft', 'with-unverified', '--include-unverified')
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['include_unverified'] is True
    assert workspace.sids['unverified'] in manifest['sources']


def test_an_export_inside_the_workspace_is_refused(workspace: _Workspace) -> None:
    target = workspace.root / 'exports' / 'sft'
    result = _syndica(workspace.root, workspace.environ, 'export', 'sft', '--output', str(target))
    assert result.returncode != 0
    assert '工作区之外' in result.stderr
    assert not target.exists()


def test_one_unreadable_trace_does_not_drop_the_rest_of_the_export(
    workspace: _Workspace, tmp_path: Path
) -> None:
    copy = tmp_path / 'ws'
    shutil.copytree(workspace.root, copy)
    _trust(copy, workspace.environ)
    sids = workspace.sids
    broken = next(
        path
        for path in (copy / '.syndica' / 'traces').iterdir()
        if path.is_dir() and sids['pass'] in (path / 'spans.jsonl').read_text()
    )
    lines = (broken / 'spans.jsonl').read_text().splitlines()
    (broken / 'spans.jsonl').write_text('{not json\n' + '\n'.join(lines[1:]) + '\n')
    out = tmp_path / 'export'
    result = _syndica(copy, workspace.environ, 'export', 'sft', '--output', str(out))
    assert result.returncode == 1, result.stderr
    rows = {str(row['id']) for row in _lines(out / 'messages.jsonl')}
    assert {sids['partial'], sids['block'], sids['retry']} <= rows
    manifest = json.loads((out / 'manifest.json').read_text())
    assert len(manifest['skipped_traces']) == 1
    assert sids['pass'] in manifest['dropped_unverified']


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
        (root / 'custom.yaml').write_text('version: 1\n', encoding='utf-8')
        environ = _environ(tmp_path / 'config')
        environ.update(
            {
                'SYNDICA_LLM_ORDER': 'openai',
                'OPENAI_BASE_URL': f'http://127.0.0.1:{port}/v1',
                'OPENAI_MODEL': 'fake',
                'OPENAI_API_KEY': 'e2e-dummy-key-0123456789',
            }
        )
        probe = _syndica(root, environ, '--config', 'custom.yaml', 'config', 'trust')
        digest = re.search(r'--sha256 ([0-9a-f]{64})', probe.stdout + probe.stderr)
        assert digest is not None, probe.stderr
        trusted = _syndica(
            root, environ, '--config', 'custom.yaml', 'config', 'trust', '--sha256', digest.group(1)
        )
        assert trusted.returncode == 0, trusted.stderr
        dataset = root / 'tasks.jsonl'
        dataset.write_text(
            '{"id": "first", "prompt": "hello one"}\n'
            'not json\n'
            '{"id": "second", "prompt": "hello two"}\n',
            encoding='utf-8',
        )
        result = _syndica(root, environ, '--config', 'custom.yaml', 'batch', str(dataset))
    finally:
        fake.terminate()
        fake.wait(timeout=5)
    assert result.returncode == 1, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[0]['line'] == 2 and rows[0]['ok'] is False
    assert rows[1:] == [{'id': 'first', 'ok': True}, {'id': 'second', 'ok': True}]
    assert len(list((root / '.syndica' / 'sessions').iterdir())) == 2
    assert 'e2e-dummy-key-0123456789' not in result.stdout + result.stderr


def test_trl_reads_the_exported_sft_file_with_its_tool_calls(workspace: _Workspace) -> None:
    pytest.importorskip('trl')
    from syndica.backends.trl.dry_run import dry_run

    out, _, _ = workspace.export('sft', 'trl')
    result = dry_run(out / 'messages.jsonl')
    assert result['rows'] == 4
    prepared = result['prepared_columns']
    assert isinstance(prepared, list) and 'input_ids' in prepared
