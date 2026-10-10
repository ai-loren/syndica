"""examples/ 下的四个 demo 离线跑一遍：开发树里的 syndica 对着脚本化的假端点
（tests/e2e/fake_openai.py 加 demo 自己的 offline/script.json）。

三个 CLI demo 原样跑 `run.sh --offline`。web demo 的 run.sh 前台起 server 等人在页面上点，这里
照它 README 的步骤走 HTTP API（建 Orchestrator 会话、发 prompt.txt、批准确认、看产物）；真浏览器
那一遍在发布验收里（scripts/acceptance.sh 的 (b)）。docs/user/demos.md 里写的 `run.sh --offline`
就是这里跑的这几个。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from syndica.cli.config import load_config

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / 'examples'
DEMOS = ('cli-self-hosting', 'web-multi-agent', 'artifact-verification', 'training-export')
_MODULES = {
    'cli-self-hosting': (),
    'web-multi-agent': ('acceptance',),
    'artifact-verification': ('acceptance',),
    'training-export': ('acceptance',),
}
_FAKE = REPO / 'tests' / 'e2e' / 'fake_openai.py'
_PREFIXES = ('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_')


def _environ(tmp_path: Path) -> dict[str, str]:
    environ = {key: value for key, value in os.environ.items() if not key.startswith(_PREFIXES)}
    environ['PATH'] = f'{Path(sys.executable).parent}{os.pathsep}{environ.get("PATH", "")}'
    environ['PYTHON'] = sys.executable
    environ['SYNDICA_PYTHON'] = sys.executable
    environ['TMPDIR'] = str(tmp_path)
    return environ


def _offline(name: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['sh', str(EXAMPLES / name / 'run.sh'), '--offline'],
        cwd=tmp_path,
        env=_environ(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=280,
    )


@pytest.mark.parametrize('name', DEMOS)
def test_every_demo_has_a_readme_a_trusted_config_inputs_and_an_offline_script(
    name: str, tmp_path: Path
) -> None:
    demo = EXAMPLES / name
    assert (demo / 'README.md').is_file() and (demo / 'run.sh').is_file()
    rules = json.loads((demo / 'offline' / 'script.json').read_text('utf-8'))['rules']
    assert rules
    inputs = [path for path in (demo / 'workspace').rglob('*') if path.is_file()]
    assert len(inputs) >= 2
    copy = tmp_path / 'ws'
    shutil.copytree(demo / 'workspace', copy)
    assert load_config(copy / '.syndica.yaml').modules == _MODULES[name]


@pytest.mark.parametrize('name', DEMOS)
def test_no_file_of_a_demo_is_ignored_by_git(name: str) -> None:
    files = [
        str(path.relative_to(REPO))
        for path in (EXAMPLES / name).rglob('*')
        if path.is_file() and '__pycache__' not in path.parts
    ]
    ignored = subprocess.run(
        ['git', 'check-ignore', '--no-index', '--stdin'],
        cwd=REPO,
        input='\n'.join(files),
        capture_output=True,
        text=True,
    )
    assert ignored.stdout.split() == [], f'这些文件进不了克隆：{ignored.stdout.split()}'
    assert len(files) >= 5


def test_the_demos_page_and_the_index_name_exactly_the_demos_run_here() -> None:
    page = (REPO / 'docs' / 'user' / 'demos.md').read_text('utf-8')
    offline = re.findall(r'^sh examples/([\w-]+)/run\.sh --offline$', page, re.M)
    assert sorted(offline) == sorted(DEMOS)
    index = (EXAMPLES / 'README.md').read_text('utf-8')
    assert sorted(re.findall(r'\]\(([\w-]+)/README\.md\)', index)) == sorted(DEMOS)


@pytest.mark.timeout(300)
def test_cli_self_hosting_fixes_the_test_in_a_copy_and_hands_off_without_committing(
    tmp_path: Path,
) -> None:
    original = (EXAMPLES / 'cli-self-hosting' / 'workspace' / 'wordcount.py').read_text('utf-8')

    completed = _offline('cli-self-hosting', tmp_path)

    said = completed.stdout + completed.stderr
    assert completed.returncode == 0, said
    assert 'FAILED (failures=2)' in completed.stdout
    for line in ('host_verify: passed', 'git: not_offered', ' M wordcount.py'):
        assert line in completed.stdout, said
    assert '__pycache__' not in completed.stdout
    workspace = Path(re.findall(r'Workspace: (\S+)', completed.stdout)[-1])
    assert 'text.split()' in (workspace / 'wordcount.py').read_text('utf-8')
    assert len(list((workspace / '.syndica' / 'handoffs').glob('*.md'))) == 1
    log = subprocess.run(
        ['git', 'log', '--format=%s'], cwd=workspace, capture_output=True, text=True, check=True
    )
    assert log.stdout.splitlines() == ['a tiny project with a failing test']
    assert '谁创建了谁' in completed.stdout
    assert (EXAMPLES / 'cli-self-hosting' / 'workspace' / 'wordcount.py').read_text() == original


@pytest.mark.timeout(300)
def test_artifact_verification_passes_with_every_layer_and_is_unverified_without_l3(
    tmp_path: Path,
) -> None:
    completed = _offline('artifact-verification', tmp_path)

    said = completed.stdout + completed.stderr
    assert completed.returncode == 0, said
    assert '== verdicts: with every layer: pass; with L3 off: unverified' in completed.stdout
    assert '验收：unverified；没能验证（outcome_check_off）' in completed.stderr
    assert re.search(r'file:notes\.md\t交付物\tdocument\t', completed.stdout), said
    assert 'R1  active  checkable  notes.md has a Highlights section' in completed.stdout
    assert 'R2  active  checkable  notes.md lists the breaking change' in completed.stdout


@pytest.mark.timeout(300)
def test_training_export_gets_four_verdicts_and_exports_all_but_the_unverified_one(
    tmp_path: Path,
) -> None:
    completed = _offline('training-export', tmp_path)

    said = completed.stdout + completed.stderr
    assert completed.returncode == 0, said
    verdicts = re.findall(r'验收：(pass|partial|block|unverified)', completed.stderr)
    assert verdicts == ['pass', 'block', 'partial', 'unverified']
    for name, rows in (('sft', 3), ('preference', 1), ('rl', 3), ('trajectory', 3)):
        line = f"{name:<11} {rows} rows; samples ['block', 'partial', 'pass']; "
        assert line + 'left out as unverified: 1' in completed.stdout, said
    try:
        import trl  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        assert 'TRL dry runs skipped' in completed.stdout
    else:
        assert 'SFTTrainer prepared 3 rows' in completed.stdout
        assert 'DPOTrainer prepared 1 pairs' in completed.stdout


# --- web：照 README 的步骤走 HTTP API -----------------------------------------------------------


@contextmanager
def _fake(script: Path) -> Iterator[int]:
    process = subprocess.Popen(
        [sys.executable, str(_FAKE)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, 'FAKE_OPENAI_SCRIPT': str(script)},
    )
    try:
        assert process.stdout is not None
        yield int(process.stdout.readline().strip())
    finally:
        process.terminate()
        process.wait(timeout=10)


def _syndica(workspace: Path, environ: dict[str, str], *args: str) -> str:
    completed = subprocess.run(
        [sys.executable, '-P', '-m', 'syndica', *args],
        cwd=workspace,
        env=environ,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return completed.stdout + completed.stderr


def _poll(read: Any, done: Any, what: str, limit: float = 60) -> Any:
    deadline = time.monotonic() + limit
    while True:
        value = read()
        if done(value):
            return value
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    pytest.fail(f'期望: {what}。实际: {limit} 秒内没有等到，最后一次读到 {value!r}。')


@pytest.mark.timeout(240)
def test_web_demo_delegates_waits_for_one_approval_and_registers_the_artifact(
    tmp_path: Path,
) -> None:
    httpx = pytest.importorskip('httpx')
    pytest.importorskip('starlette')
    from websockets.sync.client import connect

    demo = EXAMPLES / 'web-multi-agent'
    workspace = tmp_path / 'ws'
    shutil.copytree(demo / 'workspace', workspace)
    (tmp_path / 'outbox').mkdir()
    with _fake(demo / 'offline' / 'script.json') as port:
        environ = {
            **_environ(tmp_path),
            'XDG_CONFIG_HOME': str(tmp_path / 'config'),
            'SYNDICA_LLM_ORDER': 'openai',
            'OPENAI_BASE_URL': f'http://127.0.0.1:{port}/v1',
            'OPENAI_MODEL': 'fake',
            'OPENAI_API_KEY': 'e2e-dummy-key-0123456789',
        }
        digest = re.search(
            r'--sha256 ([0-9a-f]{64})', _syndica(workspace, environ, 'config', 'trust')
        )
        assert digest is not None
        _syndica(workspace, environ, 'config', 'trust', '--sha256', digest.group(1))
        up = _syndica(workspace, environ, 'up', '--no-open', '--api-token')
        try:
            located = re.search(r'http://127\.0\.0\.1:(\d+)/#login=', up)
            token = re.search(r'^[A-Za-z0-9_-]{40,}$', up, re.M)
            assert located is not None and token is not None, up
            base = f'http://127.0.0.1:{located.group(1)}'
            client = httpx.Client(
                base_url=base, headers={'Authorization': f'Bearer {token.group(0)}'}, timeout=30
            )
            sid = client.post('/api/v1/sessions', json={'role': 'Orchestrator'}).json()['sid']
            with connect(f'ws://127.0.0.1:{located.group(1)}/api/v1/sessions/{sid}/rpc') as socket:
                socket.send(json.dumps({'op': 'auth', 'id': 0, 'token': token.group(0)}))
                assert json.loads(socket.recv(timeout=30))['ok'] is True
                prompt = (demo / 'prompt.txt').read_text('utf-8').strip()
                socket.send(json.dumps({'op': 'send', 'id': 1, 'text': prompt}))
                assert json.loads(socket.recv(timeout=30))['ok'] is True

                def snapshot() -> dict[str, Any]:
                    return client.get(f'/api/v1/sessions/{sid}/snapshot').json()

                pending = _poll(
                    lambda: snapshot()['pending'],
                    lambda items: any(item['kind'] == 'confirmation' for item in items),
                    'Worker 的 cp 等人确认',
                )
                [asked] = pending
                assert not (tmp_path / 'outbox' / 'incident.md').exists()
                approve = {'op': 'answer', 'id': 2, 'request_id': asked['request_id']}
                socket.send(json.dumps({**approve, 'text': 'approve'}))
                assert json.loads(socket.recv(timeout=30))['ok'] is True
                final = _poll(
                    snapshot,
                    lambda shot: (
                        'incident.md'
                        in ''.join(
                            block['text']
                            for block in shot['transcripts'][shot['root_id']]['blocks']
                            if block['role'] == 'assistant'
                        )
                    ),
                    'Orchestrator 拿到 Worker 的结果之后答完',
                )
            assert sorted(agent['name'] for agent in final['agents']) == ['Orchestrator', 'Worker']
            assert (tmp_path / 'outbox' / 'incident.md').is_file()
            listed = client.get(f'/api/v1/sessions/{sid}/artifacts').json()['artifacts']
            assert [item['path'] for item in listed] == ['incident.md']
        finally:
            _syndica(workspace, environ, 'down')
