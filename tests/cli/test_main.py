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
    'GEMINI_API_KEY',
    'GEMINI_MODEL',
    'OPENAI_API_KEY',
    'OPENAI_BASE_URL',
    'OPENAI_MODEL',
    'OPENROUTER_API_KEY',
    'OPENROUTER_MODEL',
    'SYNDICA_LLM_ORDER',
}


def _without_model_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _MODEL_ENV}


class _Host:
    def __init__(self) -> None:
        self.requests: list[RunRequest] = []
        self.inspections: list[InspectRequest] = []
        self.doctor_configs: list[str] = []
        self.doctor_verify: list[bool] = []
        self.chat_turn_limits: list[int | None] = []

    def run_request(self, request: RunRequest) -> str:
        self.requests.append(request)
        return 'completed'

    def inspect(self, request: InspectRequest) -> str:
        self.inspections.append(request)
        return f'{request.resource}:{request.action}:{request.name or ""}'

    def doctor(self, config: str, *, verify: bool = False) -> str:
        self.doctor_configs.append(config)
        self.doctor_verify.append(verify)
        return 'healthy'

    def open_chat(self, config: str, max_turns: int | None = None) -> str:
        assert config == '.syndica.yaml'
        self.chat_turn_limits.append(max_turns)
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
            '--no-write',
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
        'no_write': False,
        'handoff': False,
        'git_mode': None,
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
        'no_write': True,
        'handoff': False,
        'git_mode': None,
    }


def test_run_help_explains_no_write() -> None:
    stdout = StringIO()

    code = main(['run', '--help'], host=_Host(), io=CliIO(StringIO(), stdout, StringIO()))

    help_text = ' '.join(stdout.getvalue().split())
    assert code == 0
    assert '--no-write' in help_text
    assert '只读' in help_text
    assert all(tool in help_text for tool in ('Read', 'Grep', 'Glob'))
    assert not any(tool in help_text for tool in ('Write', 'Edit', 'Bash'))


def test_no_write_with_a_session_fails_before_reaching_the_host() -> None:
    host = _Host()
    stderr = StringIO()

    code = main(
        ['run', 'go', '--no-write', '--session', 'agent-1'],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
    )

    assert code == 2
    assert '期望: --no-write 用于一次新的 run' in stderr.getvalue()
    assert '--session agent-1' in stderr.getvalue()
    assert '去掉 --session' in stderr.getvalue()
    assert host.requests == []


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
    assert host.doctor_verify == [False]
    assert stdout.getvalue() == 'healthy\n'


def test_doctor_verify_routes_the_flag() -> None:
    host = _Host()

    code = main(
        ['doctor', '--verify'],
        host=host,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
    )

    assert code == 0
    assert host.doctor_verify == [True]


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


@pytest.mark.parametrize('value', ['0', '-2', 'many'])
def test_invalid_chat_turn_limit_fails_before_opening_a_session(value: str) -> None:
    host = _Host()
    stderr = StringIO()

    code = main(
        ['chat', '--max-turns', value],
        host=host,
        io=CliIO(stdin=StringIO(':quit\n'), stdout=StringIO(), stderr=stderr),
    )

    assert code == 2
    assert '--max-turns' in stderr.getvalue()
    assert '下一步:' in stderr.getvalue()
    assert host.chat_turn_limits == []


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
    assert host.chat_turn_limits == [None]
    assert [(request.prompt, request.session) for request in host.requests] == [
        ('first message', 'root-agent-id'),
        ('second message', 'worker-agent-id'),
    ]
    assert '[root-agent-id]> ' in stdout.getvalue()
    assert '[worker-agent-id]> ' in stdout.getvalue()
    assert 'worker-agent-id Worker zombie' in stdout.getvalue()


def test_chat_forwards_its_turn_limit_to_the_host() -> None:
    host = _Host()

    code = main(
        ['chat', '--max-turns', '4'],
        host=host,
        io=CliIO(stdin=StringIO(':quit\n'), stdout=StringIO(), stderr=StringIO()),
    )

    assert code == 0
    assert host.chat_turn_limits == [4]


def test_module_host_forwards_the_chat_turn_limit_to_the_runtime_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import syndica.cli.host as host_module
    from syndica.__main__ import _LazyHost  # pyright: ignore[reportPrivateUsage]

    opened: list[tuple[str, int | None]] = []

    class RecordingHost:
        def __init__(self, **_: object) -> None:
            pass

        def open_chat(self, config: str, max_turns: int | None = None) -> str:
            opened.append((config, max_turns))
            return 'root-agent-id'

    monkeypatch.setattr(host_module, 'RuntimeCliHost', RecordingHost)
    lazy = _LazyHost(CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()))

    assert lazy.open_chat('.syndica.yaml', 3) == 'root-agent-id'
    assert opened == [('.syndica.yaml', 3)]


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
    lines = completed.stdout.splitlines()
    assert lines[:3] == [
        'config: ok .syndica.yaml',
        'models: ok default',
        f'workspace: ok {tmp_path}',
    ]
    assert _agent_env_names(lines[3]) == _EXPECTED_AGENT_ENV
    assert completed.stderr == ''


# doctor 的 env 行：固定白名单里宿主实际有的，加宿主自己设的四个；任何 *_KEY 都不在里面。
_HOST_SET = ['GIT_TERMINAL_PROMPT', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GH_CONFIG_DIR']
_HOST_SET += ['GH_TOKEN', 'GH_ENTERPRISE_TOKEN']
_FIXED = ['PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR']
_FIXED += ['XDG_CACHE_HOME', 'UV_CACHE_DIR']
_EXPECTED_AGENT_ENV = [name for name in _FIXED if name in os.environ] + _HOST_SET


def _agent_env_names(line: str) -> list[str]:
    assert line.startswith('env: ')
    return line.removeprefix('env: ').split()


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
    lines = completed.stdout.splitlines()
    assert lines[:3] == [
        'config: ok .syndica.yaml',
        'models: ok default',
        f'workspace: ok {tmp_path}',
    ]
    assert _agent_env_names(lines[3]) == _EXPECTED_AGENT_ENV
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


def _only(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key in _MODEL_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_gemini_configuration_uses_its_openai_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.backends.llm_router import ModelRouter

    _only(monkeypatch, GEMINI_API_KEY='test-gemini-key')

    llm = _configured_model('default')

    assert isinstance(llm, ModelRouter)
    assert [health.name for health in llm.health()] == ['gemini']
    assert llm.config.model == 'gemini-3.5-flash-lite'
    assert llm.config.client_args == {
        'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai',
        'api_key': 'test-gemini-key',
        'foreign_tool_call_extra': {
            'google': {'thought_signature': 'skip_thought_signature_validator'}
        },
    }


def test_gemini_model_can_be_chosen_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    from syndica.__main__ import _configured_model

    _only(monkeypatch, GEMINI_API_KEY='test-gemini-key', GEMINI_MODEL='gemini-3.8-flash')

    assert _configured_model('default').config.model == 'gemini-3.8-flash'


def test_a_gemini_model_without_a_key_is_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.kernel import ConfigError

    _only(monkeypatch, GEMINI_MODEL='gemini-3.8-flash')

    with pytest.raises(ConfigError) as caught:
        _configured_model('default')

    assert 'GEMINI_API_KEY' in caught.value.expected
    assert 'GEMINI_API_KEY' in caught.value.next_action


def test_gemini_and_openrouter_form_a_ring_with_gemini_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.backends.llm_openai import recoverable, retry_after
    from syndica.backends.llm_router import ModelRouter

    _only(monkeypatch, GEMINI_API_KEY='test-gemini-key', OPENROUTER_API_KEY='test-openrouter-key')

    llm = _configured_model('default')

    assert isinstance(llm, ModelRouter)
    assert [health.name for health in llm.health()] == ['gemini', 'openrouter']
    assert llm.config.model == 'gemini-3.5-flash-lite'
    assert llm._retry_after is retry_after  # pyright: ignore[reportPrivateUsage]
    assert llm._recoverable is recoverable  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ('env', 'platform'),
    [
        ({'GEMINI_API_KEY': 'test-gemini-key'}, 'gemini'),
        ({'OPENROUTER_API_KEY': 'test-openrouter-key'}, 'openrouter'),
        ({'OPENAI_BASE_URL': 'https://llm.example.test/v1', 'OPENAI_MODEL': 'm'}, 'openai'),
    ],
)
def test_a_single_platform_is_retried_through_the_router(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    platform: str,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.backends.llm_openai import recoverable, retry_after
    from syndica.backends.llm_router import ModelRouter

    _only(monkeypatch, **env)

    llm = _configured_model('default')

    assert isinstance(llm, ModelRouter)
    assert [health.name for health in llm.health()] == [platform]
    assert llm._retry_after is retry_after  # pyright: ignore[reportPrivateUsage]
    assert llm._recoverable is recoverable  # pyright: ignore[reportPrivateUsage]


def test_an_explicit_model_name_is_retried_on_its_own_platform_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.backends.llm_openai import recoverable
    from syndica.backends.llm_router import ModelRouter

    _only(monkeypatch, GEMINI_API_KEY='test-gemini-key', OPENROUTER_API_KEY='test-openrouter-key')

    llm = _configured_model('cohere/north-mini-code:free')

    assert isinstance(llm, ModelRouter)
    assert [health.name for health in llm.health()] == ['openrouter']
    assert llm.config.model == 'cohere/north-mini-code:free'
    assert llm._recoverable is recoverable  # pyright: ignore[reportPrivateUsage]


def test_the_ring_follows_syndica_llm_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from syndica.__main__ import _configured_model
    from syndica.backends.llm_router import ModelRouter

    _only(
        monkeypatch,
        GEMINI_API_KEY='test-gemini-key',
        OPENROUTER_API_KEY='test-openrouter-key',
        OPENAI_BASE_URL='https://llm.example.test/v1',
        OPENAI_MODEL='model-a',
        SYNDICA_LLM_ORDER='openai, openrouter,gemini',
    )

    llm = _configured_model('default')

    assert isinstance(llm, ModelRouter)
    assert [health.name for health in llm.health()] == ['openai', 'openrouter', 'gemini']


def test_generic_openai_variables_stay_out_of_the_default_ring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.backends.llm_router import ModelRouter

    _only(
        monkeypatch,
        GEMINI_API_KEY='test-gemini-key',
        OPENROUTER_API_KEY='test-openrouter-key',
        OPENAI_BASE_URL='https://paid.example.test/v1',
        OPENAI_MODEL='paid-model',
    )

    llm = _configured_model('default')

    assert isinstance(llm, ModelRouter)
    assert [health.name for health in llm.health()] == ['gemini', 'openrouter']


@pytest.mark.parametrize(
    ('order', 'needle'),
    [
        ('gemini,claude', "'claude'"),
        ('gemini,gemini', '重复'),
        (' , ', '空'),
        ('gemini,openrouter', 'OPENROUTER_API_KEY'),
        ('openai', 'OPENAI_BASE_URL'),
    ],
)
def test_an_unusable_ring_order_is_rejected_with_the_fix(
    monkeypatch: pytest.MonkeyPatch,
    order: str,
    needle: str,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.kernel import ConfigError

    _only(monkeypatch, GEMINI_API_KEY='test-gemini-key', SYNDICA_LLM_ORDER=order)

    with pytest.raises(ConfigError) as caught:
        _configured_model('default')

    error = caught.value
    assert needle in f'{error.expected}{error.received}{error.next_action}'
    assert 'SYNDICA_LLM_ORDER' in f'{error.expected}{error.received}'


def test_with_no_platform_configured_the_error_offers_every_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.kernel import ConfigError

    _only(monkeypatch)

    with pytest.raises(ConfigError) as caught:
        _configured_model('default')

    next_action = caught.value.next_action
    assert 'OPENAI_BASE_URL' in next_action
    assert 'GEMINI_API_KEY' in next_action
    assert 'OPENROUTER_API_KEY' in next_action


def test_an_explicit_model_name_is_never_sent_to_gemini_and_the_error_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syndica.__main__ import _configured_model
    from syndica.kernel import ConfigError

    _only(monkeypatch, GEMINI_API_KEY='test-gemini-key', GEMINI_MODEL='gemini-3.8-flash')

    with pytest.raises(ConfigError) as caught:
        _configured_model('gemini-3.8-pro')

    next_action = caught.value.next_action
    assert 'GEMINI_API_KEY' not in next_action
    assert 'OPENROUTER_API_KEY' in next_action
    assert 'default' in next_action
    assert 'GEMINI_MODEL' in next_action


def test_module_entrypoint_doctor_builds_a_two_platform_ring_without_network(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'doctor'],
        cwd=tmp_path,
        env={
            **_without_model_env(),
            'GEMINI_API_KEY': 'must-not-appear',
            'OPENROUTER_API_KEY': 'must-not-appear-either',
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines()[:2] == ['config: ok .syndica.yaml', 'models: ok default']
    assert 'must-not-appear' not in completed.stdout + completed.stderr


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
