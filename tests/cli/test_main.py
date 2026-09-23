"""The CLI router is deterministic and keeps the version path dependency-free."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import asdict
from io import StringIO
from pathlib import Path

import pytest

from syndica.cli.main import CliIO, InspectRequest, RunRequest, main

REPO_ROOT = Path(__file__).parents[2]
_MODEL_ENV = {
    'OPENAI_API_KEY',
    'OPENAI_BASE_URL',
    'OPENAI_MODEL',
    'OPENROUTER_API_KEY',
    'OPENROUTER_MODEL',
}


def _without_model_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _MODEL_ENV}


class _Host:
    def __init__(self) -> None:
        self.requests: list[RunRequest] = []
        self.inspections: list[InspectRequest] = []
        self.doctor_configs: list[str] = []

    def run_request(self, request: RunRequest) -> str:
        self.requests.append(request)
        return 'completed'

    def inspect(self, request: InspectRequest) -> str:
        self.inspections.append(request)
        return f'{request.resource}:{request.action}:{request.name or ""}'

    def doctor(self, config: str) -> str:
        self.doctor_configs.append(config)
        return 'healthy'

    def open_chat(self, config: str) -> str:
        assert config == '.syndica.yaml'
        return 'root-agent-id'

    def active_agents(self) -> str:
        return 'root-agent-id Orchestrator zombie\nworker-agent-id Worker zombie'


def test_module_version_command_does_not_import_product_packages() -> None:
    script = """
import json
import runpy
import sys

sys.argv = ['syndica', 'version']
try:
    runpy.run_module('syndica', run_name='__main__')
except SystemExit as exc:
    code = exc.code
else:
    code = None
loaded = sorted(
    name
    for name in sys.modules
    if name == 'yaml'
    or name.startswith('yaml.')
    or name.startswith('syndica.kernel')
    or name.startswith('syndica.backends')
)
print(json.dumps({'code': code, 'loaded': loaded}))
"""
    completed = subprocess.run(
        [sys.executable, '-c', script],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    lines = completed.stdout.splitlines()
    assert lines[0] == 'syndica 0.0.0'
    assert json.loads(lines[1]) == {'code': 0, 'loaded': []}
    assert completed.stderr == ''


def test_default_and_explicit_run_share_one_request_shape() -> None:
    host = _Host()
    implicit_out = StringIO()
    explicit_out = StringIO()

    implicit_code = main(
        ['write', 'the report'],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=implicit_out, stderr=StringIO()),
    )
    explicit_code = main(
        [
            '--config',
            'team.yaml',
            'run',
            'review this',
            '--agent',
            'Worker',
            '--max-turns',
            '7',
            '--max-cost',
            '1.5',
            '--max-tokens',
            '4096',
            '--max-wall-clock',
            '30',
            '--flag',
            'retry_enabled=true',
            '--flag',
            'attempts=3',
        ],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=explicit_out, stderr=StringIO()),
    )

    assert (implicit_code, explicit_code) == (0, 0)
    assert implicit_out.getvalue() == 'completed\n'
    assert explicit_out.getvalue() == 'completed\n'
    assert asdict(host.requests[0]) == {
        'prompt': 'write the report',
        'config': '.syndica.yaml',
        'agent': None,
        'topology': None,
        'session': None,
        'max_turns': None,
        'max_cost': None,
        'max_tokens': None,
        'max_wall_clock': None,
        'flags': (),
    }
    assert asdict(host.requests[1]) == {
        'prompt': 'review this',
        'config': 'team.yaml',
        'agent': 'Worker',
        'topology': None,
        'session': None,
        'max_turns': 7,
        'max_cost': 1.5,
        'max_tokens': 4096,
        'max_wall_clock': 30.0,
        'flags': ('retry_enabled=true', 'attempts=3'),
    }


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (['agents', 'list'], ('agents', 'list', None)),
        (['agents', 'show', 'Worker'], ('agents', 'show', 'Worker')),
        (['topology', 'list'], ('topology', 'list', None)),
        (['topology', 'show', 'review'], ('topology', 'show', 'review')),
        (['flags', 'list'], ('flags', 'list', None)),
        (['flags', 'show', 'retry_enabled'], ('flags', 'show', 'retry_enabled')),
    ],
)
def test_noun_verb_commands_route_through_one_inspection_request(
    argv: list[str],
    expected: tuple[str, str, str | None],
) -> None:
    host = _Host()
    stdout = StringIO()

    code = main(
        argv,
        host=host,
        io=CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO()),
    )

    assert code == 0
    assert (
        host.inspections[0].resource,
        host.inspections[0].action,
        host.inspections[0].name,
    ) == expected
    assert stdout.getvalue() == f'{expected[0]}:{expected[1]}:{expected[2] or ""}\n'


def test_noun_help_lists_verbs_and_missing_verb_is_not_a_default_prompt() -> None:
    host = _Host()
    help_out = StringIO()
    missing_err = StringIO()

    help_code = main(
        ['agents', '--help'],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=help_out, stderr=StringIO()),
    )
    missing_code = main(
        ['agents'],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=missing_err),
    )

    assert (help_code, missing_code) == (0, 2)
    assert 'list' in help_out.getvalue()
    assert 'show' in help_out.getvalue()
    assert 'required' in missing_err.getvalue()
    assert host.requests == []


def test_leading_unknown_option_is_not_guessed_as_a_prompt() -> None:
    host = _Host()
    stderr = StringIO()

    code = main(
        ['--unknown'],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
    )

    assert code == 2
    assert '--unknown' in stderr.getvalue()
    assert host.requests == []


def test_doctor_routes_with_the_selected_config() -> None:
    host = _Host()
    stdout = StringIO()

    code = main(
        ['--config=custom.yml', 'doctor'],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO()),
    )

    assert code == 0
    assert host.doctor_configs == ['custom.yml']
    assert stdout.getvalue() == 'healthy\n'


@pytest.mark.parametrize('verb', ['list', 'show', 'resume', 'rm'])
def test_session_commands_fail_with_the_milestone_boundary(verb: str) -> None:
    argv = ['sessions', verb]
    if verb != 'list':
        argv.append('session-1')
    stderr = StringIO()

    code = main(
        argv,
        host=_Host(),
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
    )

    assert code == 2
    assert 'SessionStore' in stderr.getvalue()
    assert '里程碑 6' in stderr.getvalue()


@pytest.mark.parametrize(
    'argv',
    [
        ['run', 'go', '--agent', 'Worker', '--topology', 'review'],
        ['run', 'go', '--agent', 'Worker', '--session', 'existing'],
        ['run', 'go', '--max-turns', '0'],
        ['run', 'go', '--max-cost', 'nan'],
        ['run', 'go', '--max-tokens', '3.5'],
        ['run', 'go', '--max-wall-clock', '-1'],
    ],
)
def test_invalid_run_arguments_fail_before_reaching_the_host(argv: list[str]) -> None:
    host = _Host()
    stderr = StringIO()

    code = main(
        argv,
        host=host,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
    )

    assert code == 2
    assert '期望:' in stderr.getvalue()
    assert '实际:' in stderr.getvalue()
    assert '下一步:' in stderr.getvalue()
    assert host.requests == []


@pytest.mark.parametrize(
    'flags',
    [
        ['missing-equals'],
        ['=value'],
        ['same=first', 'same=second'],
    ],
)
def test_malformed_or_duplicate_flags_fail_before_reaching_the_host(
    flags: list[str],
) -> None:
    host = _Host()
    stderr = StringIO()
    argv = ['run', 'go']
    for flag in flags:
        argv.extend(['--flag', flag])

    code = main(
        argv,
        host=host,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
    )

    assert code == 2
    assert 'key=value' in stderr.getvalue()
    assert '下一步:' in stderr.getvalue()
    assert host.requests == []


def test_console_entry_and_cli_public_surface_are_fixed() -> None:
    import syndica.cli as cli

    pyproject = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert pyproject['project']['scripts'] == {'syndica': 'syndica.__main__:entrypoint'}
    assert cli.__all__ == ['CliHost', 'CliIO', 'InspectRequest', 'RunRequest', 'main']
    assert cli.main is main


def test_host_configuration_errors_reach_stderr_with_a_nonzero_exit() -> None:
    class HostError(Exception):
        expected = 'a configured agent'
        received = 'the requested agent is missing'
        next_action = 'run syndica agents list'

    class FailingHost(_Host):
        def run_request(self, request: RunRequest) -> str:
            raise HostError

    stderr = StringIO()

    code = main(
        ['run', 'go'],
        host=FailingHost(),
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
    )

    assert code == 1
    assert stderr.getvalue() == (
        '期望: a configured agent\n'
        '实际: the requested agent is missing\n'
        '下一步: run syndica agents list\n'
    )


def test_unexpected_host_errors_are_wrapped_without_a_traceback() -> None:
    class FailingHost(_Host):
        def run_request(self, request: RunRequest) -> str:
            raise RuntimeError('boom')

    stderr = StringIO()

    code = main(
        ['run', 'go'],
        host=FailingHost(),
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
    )

    assert code == 1
    assert stderr.getvalue() == (
        '期望: CLI host 完成当前命令\n'
        '实际: RuntimeError: boom\n'
        '下一步: 检查 Runtime host 配置和错误日志后重试\n'
    )


def test_chat_uses_run_requests_and_keeps_the_current_target_visible() -> None:
    host = _Host()
    stdout = StringIO()

    code = main(
        ['chat'],
        host=host,
        io=CliIO(
            stdin=StringIO('first message\n:agents\n:use worker-agent-id\nsecond message\n:quit\n'),
            stdout=stdout,
            stderr=StringIO(),
        ),
    )

    assert code == 0
    assert [(request.prompt, request.session) for request in host.requests] == [
        ('first message', 'root-agent-id'),
        ('second message', 'worker-agent-id'),
    ]
    assert '[root-agent-id]> ' in stdout.getvalue()
    assert '[worker-agent-id]> ' in stdout.getvalue()
    assert 'worker-agent-id Worker zombie' in stdout.getvalue()


def test_module_entrypoint_lists_agents_without_model_configuration(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'agents', 'list'],
        cwd=tmp_path,
        env=_without_model_env(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == ['Planner', 'Explorer', 'Worker', 'Orchestrator']
    assert completed.stderr == ''


def test_module_entrypoint_doctor_builds_the_openai_composition_without_network(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'doctor'],
        cwd=tmp_path,
        env={
            **_without_model_env(),
            'OPENAI_BASE_URL': 'https://llm.example.test/v1',
            'OPENAI_API_KEY': 'test-key',
            'OPENAI_MODEL': 'model-a',
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [
        'config: ok .syndica.yaml',
        'models: ok default',
        f'workspace: ok {tmp_path}',
    ]
    assert completed.stderr == ''


@pytest.mark.parametrize('model', ['openrouter/free', 'meta-llama/example:free'])
def test_module_entrypoint_doctor_accepts_only_explicitly_free_openrouter_models(
    tmp_path: Path,
    model: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'doctor'],
        cwd=tmp_path,
        env={
            **_without_model_env(),
            'OPENROUTER_API_KEY': 'test-openrouter-key',
            'OPENROUTER_MODEL': model,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [
        'config: ok .syndica.yaml',
        'models: ok default',
        f'workspace: ok {tmp_path}',
    ]
    assert completed.stderr == ''


def test_openrouter_configuration_uses_its_endpoint_key_and_attribution_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model

    for key in _MODEL_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-openrouter-key')

    llm = _configured_model('default')

    assert llm.config.model == 'openrouter/free'
    assert llm.config.client_args == {
        'base_url': 'https://openrouter.ai/api/v1',
        'api_key': 'test-openrouter-key',
        'headers': {
            'HTTP-Referer': 'https://github.com/ai-loren/syndica',
            'X-Title': 'Syndica',
        },
    }


def test_module_entrypoint_rejects_a_paid_openrouter_model_before_network(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'doctor'],
        cwd=tmp_path,
        env={
            **_without_model_env(),
            'OPENROUTER_API_KEY': 'must-not-appear',
            'OPENROUTER_MODEL': 'anthropic/paid-model',
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ''
    assert 'openrouter/free' in completed.stderr
    assert ':free' in completed.stderr
    assert 'anthropic/paid-model' in completed.stderr
    assert 'must-not-appear' not in completed.stderr


def test_generic_openai_variables_cannot_bypass_the_openrouter_free_model_gate(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'doctor'],
        cwd=tmp_path,
        env={
            **_without_model_env(),
            'OPENAI_BASE_URL': 'https://openrouter.ai/api/v1',
            'OPENAI_API_KEY': 'must-not-appear',
            'OPENAI_MODEL': 'anthropic/paid-model',
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ''
    assert 'openrouter/free' in completed.stderr
    assert ':free' in completed.stderr
    assert 'must-not-appear' not in completed.stderr


def test_module_entrypoint_requires_an_openrouter_key_when_its_model_is_configured(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'doctor'],
        cwd=tmp_path,
        env={
            **_without_model_env(),
            'OPENROUTER_MODEL': 'openrouter/free',
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ''
    assert 'OPENROUTER_API_KEY' in completed.stderr
