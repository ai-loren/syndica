"""`syndica export --output` 不跟着符号链接写、不写进工作区；读不出来的 trace 头与会话元数据记进
manifest，退出码 1。

工作区由真的宿主与 Runtime 用脚本化模型跑出来（一条 pass、一条 block）；导出走真的
`python -m syndica` 子进程，导入的是这份测试所在的工作副本的 `src`。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pytest

from syndica.__main__ import _Training  # pyright: ignore[reportPrivateUsage]
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, CliOutcome, RunRequest
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn

_SRC = Path(__file__).resolve().parents[2] / 'src'
_GOAL = 'ship a report with a section four'
_RUBRIC = {
    'id': 'R1',
    'statement': 'the report has a section four',
    'kind': 'checkable',
    'anchors': [{'source': 'user_message', 'locator': 'msg:goal', 'quote': 'section four'}],
}
_CONFIG = (
    'version: 1\n'
    'modules: [acceptance]\n'
    'flags:\n'
    '  acceptance_rubrics: true\n'
    '  acceptance_outcome: true\n'
    '  acceptance_outcome_block_retries: 0\n'
    '  acceptance_outcome_partial_retries: 0\n'
)


def _outcome(result: str, quote: str) -> ScriptedTurn:
    item = {
        'rubric_id': 'R1',
        'result': result,
        'evidence': [{'source': 'user_message', 'locator': 'reply', 'quote': quote}],
    }
    return ScriptedTurn(content=json.dumps({'outcomes': [item], 'reason': 'checked'}))


def _projection() -> ScriptedTurn:
    return ScriptedTurn(content=json.dumps({'rubrics': [_RUBRIC]}))


class _Lazy:
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
    inherited = environ.get('PYTHONPATH')
    environ['PYTHONPATH'] = os.pathsep.join([str(_SRC), *([inherited] if inherited else [])])
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


def _sessions_by_creation(root: Path) -> list[str]:
    metas = [
        json.loads((path / 'meta.json').read_text(encoding='utf-8'))['meta']
        for path in (root / '.syndica' / 'sessions').iterdir()
    ]
    return [meta['sid'] for meta in sorted(metas, key=lambda meta: meta['created_at'])]


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _manifest(out: Path) -> dict[str, list[object]]:
    return json.loads((out / 'manifest.json').read_text(encoding='utf-8'))


@dataclass(frozen=True)
class _Workspace:
    root: Path
    sids: dict[str, str]
    environ: dict[str, str]

    def export(self, output: Path) -> subprocess.CompletedProcess[str]:
        return _syndica(self.root, self.environ, 'export', 'sft', '--output', str(output))

    def trace_of(self, sid: str) -> Path:
        [found] = [
            path
            for path in (self.root / '.syndica' / 'traces').iterdir()
            if path.is_dir() and sid in (path / 'spans.jsonl').read_text(encoding='utf-8')
        ]
        return found


@pytest.fixture(scope='module')
def workspace(tmp_path_factory: pytest.TempPathFactory) -> _Workspace:
    root = tmp_path_factory.mktemp('export-safety')
    config = root / '.syndica.yaml'
    config.write_text(_CONFIG, encoding='utf-8')
    tasks = [
        (
            'pass',
            [
                _projection(),
                ScriptedTurn(content='Final answer: the report now has a section four.'),
                _outcome('satisfied', 'the report now has'),
            ],
        ),
        (
            'block',
            [
                _projection(),
                ScriptedTurn(content='Final answer: nothing was written.'),
                _outcome('unmet', 'nothing was written'),
            ],
        ),
    ]
    dataset = root / 'tasks.jsonl'
    dataset.write_text(
        ''.join(
            json.dumps({'id': name, 'prompt': 'write the report', 'agent': 'Worker', 'goal': _GOAL})
            + '\n'
            for name, _ in tasks
        ),
        encoding='utf-8',
    )
    llm = ScriptedLLM([turn for _, script in tasks for turn in script])
    stderr = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda _: llm,
        workspace_root=root,
        environ=dict(os.environ),
    )
    try:
        outcome = _Training(_Lazy(host)).batch(str(dataset), str(config))  # pyright: ignore[reportArgumentType]
    finally:
        host.close()
    assert outcome.exit_code == 0, outcome.output + stderr.getvalue()
    reported = re.findall(r'验收：(pass|partial|block|unverified)', stderr.getvalue())
    assert reported == ['pass', 'block'], stderr.getvalue()
    sids = dict(zip([name for name, _ in tasks], _sessions_by_creation(root), strict=True))
    environ = _environ(tmp_path_factory.mktemp('export-safety-config'))
    _trust(root, environ)
    return _Workspace(root, sids, environ)


@pytest.fixture
def copied(workspace: _Workspace, tmp_path: Path) -> _Workspace:
    root = tmp_path / 'ws'
    shutil.copytree(workspace.root, root)
    _trust(root, workspace.environ)
    return _Workspace(root, workspace.sids, workspace.environ)


@pytest.mark.parametrize('target_exists', [True, False], ids=['to_an_empty_dir', 'dangling'])
def test_an_output_that_is_a_symlink_is_refused_and_nothing_lands_where_it_points(
    workspace: _Workspace, tmp_path: Path, target_exists: bool
) -> None:
    target = tmp_path / 'target'
    if target_exists:
        target.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(target, target_is_directory=True)

    result = workspace.export(link)

    assert result.returncode != 0, result.stdout
    assert f'{link} 是符号链接' in result.stderr
    assert '不是符号链接' in result.stderr
    if target_exists:
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()


def test_an_output_under_tmp_is_written(workspace: _Workspace) -> None:
    # 不 resolve：macOS 上 /tmp 是指向 /private/tmp 的符号链接，这里要的正是「上级是链接」。
    parent = Path(tempfile.mkdtemp(dir='/tmp'))
    try:
        out = parent / 'sft'
        result = workspace.export(out)
        assert result.returncode == 0, result.stdout + result.stderr
        rows = {str(row['id']) for row in _lines(out / 'messages.jsonl')}
        assert rows == set(workspace.sids.values())
    finally:
        shutil.rmtree(parent)


def test_an_output_under_a_symlinked_parent_is_written_where_the_parent_points(
    workspace: _Workspace, tmp_path: Path
) -> None:
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'alias').symlink_to(real, target_is_directory=True)

    result = workspace.export(tmp_path / 'alias' / 'sft')

    assert result.returncode == 0, result.stdout + result.stderr
    rows = {str(row['id']) for row in _lines(real / 'sft' / 'messages.jsonl')}
    assert rows == set(workspace.sids.values())


def test_an_output_under_a_symlinked_parent_that_points_into_the_workspace_is_refused(
    workspace: _Workspace, tmp_path: Path
) -> None:
    (tmp_path / 'into').symlink_to(workspace.root, target_is_directory=True)

    result = workspace.export(tmp_path / 'into' / 'via-link' / 'sft')

    assert result.returncode != 0, result.stdout
    assert '工作区之外' in result.stderr
    assert not (workspace.root / 'via-link').exists()


def test_an_output_spelled_in_other_letter_case_inside_the_workspace_is_refused(
    workspace: _Workspace,
) -> None:
    variant = workspace.root.parent / workspace.root.name.swapcase()
    assert str(variant) != str(workspace.root)
    if not variant.exists() or not os.path.samefile(variant, workspace.root):
        pytest.skip(
            f'文件系统区分大小写：{variant} 与 {workspace.root} 不是同一个目录，'
            '换了大小写的路径进不了工作区'
        )

    result = workspace.export(variant / 'exports' / 'sft')

    assert result.returncode != 0, result.stdout
    assert '工作区之外' in result.stderr
    assert '~/datasets' in result.stderr
    assert not (workspace.root / 'exports').exists()


@pytest.mark.parametrize(
    ('newer', 'next_action'),
    [(False, '删掉这个目录'), (True, '不要删掉')],
    ids=['corrupt', 'newer_schema'],
)
def test_a_trace_whose_header_cannot_be_read_exits_1_and_is_named_in_skipped_traces(
    copied: _Workspace, tmp_path: Path, newer: bool, next_action: str
) -> None:
    sids = copied.sids
    broken = copied.trace_of(sids['pass'])
    header = broken / 'trace.json'
    if newer:
        value = json.loads(header.read_text(encoding='utf-8'))
        value['schema'] = 'syndica.trace/99'
        header.write_text(json.dumps(value), encoding='utf-8')
    else:
        header.write_text('{not json\n', encoding='utf-8')
    out = tmp_path / 'export'

    result = copied.export(out)

    assert result.returncode == 1, result.stdout + result.stderr
    assert '1 个 trace 读不回来' in result.stdout
    manifest = _manifest(out)
    [entry] = manifest['skipped_traces']
    assert str(broken) in str(entry) and next_action in str(entry)
    assert manifest['failed'] == []
    assert manifest['dropped_unverified'] == [sids['pass']]
    assert {str(row['id']) for row in _lines(out / 'messages.jsonl')} == {sids['block']}


@pytest.mark.parametrize(
    ('newer', 'next_action'),
    [(False, '删掉它所在的目录'), (True, '用新版 syndica 读它')],
    ids=['corrupt', 'newer_schema'],
)
def test_a_session_whose_meta_cannot_be_read_exits_1_and_is_named_in_failed(
    copied: _Workspace, tmp_path: Path, newer: bool, next_action: str
) -> None:
    sids = copied.sids
    meta = copied.root / '.syndica' / 'sessions' / sids['pass'] / 'meta.json'
    if newer:
        value = json.loads(meta.read_text(encoding='utf-8'))
        value['schema'] = 'syndica.session/99'
        meta.write_text(json.dumps(value), encoding='utf-8')
    else:
        meta.write_text('{not json\n', encoding='utf-8')
    out = tmp_path / 'export'

    result = copied.export(out)

    assert result.returncode == 1, result.stdout + result.stderr
    assert '1 条会话没拼成' in result.stdout
    manifest = _manifest(out)
    [entry] = manifest['failed']
    assert str(entry).startswith(f'{sids["pass"]}: {meta} ')
    assert next_action in str(entry)
    assert manifest['skipped_traces'] == []
    assert {str(row['id']) for row in _lines(out / 'messages.jsonl')} == {sids['block']}
