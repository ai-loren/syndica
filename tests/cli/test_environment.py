"""Agent 的命令环境（spec D6）：经公开的 CLI 宿主与脚本化 LLM，看 Agent 的 Bash 里到底有什么。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from io import StringIO
from pathlib import Path

import pytest

from syndica.cli.environment import FIXED_PASS_ENV, agent_environment
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.kernel import ConfigError, ScriptedLLM, ScriptedTurn, ToolCall

_SECRETS = {
    'GEMINI_API_KEY': 'g-secret',
    'OPENROUTER_API_KEY': 'o-secret',
    'SSH_AUTH_SOCK': '/tmp/agent.sock',
    'FOO_TOKEN': 'f-secret',
}
# 逐字写死，不从 FIXED_PASS_ENV 推导：从被测的表推导期望值，删掉表里一项时期望值跟着缩水。
_WHITELIST = ['PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR']
_WHITELIST += ['XDG_CACHE_HOME', 'UV_CACHE_DIR']
_HOST_SET = ['GIT_TERMINAL_PROMPT', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GH_CONFIG_DIR']
_HOST_SET += ['GH_TOKEN', 'GH_ENTERPRISE_TOKEN']
_HOST_ENV = {
    **os.environ,
    'LANG': 'C.UTF-8',
    'LC_ALL': 'C.UTF-8',
    'LC_CTYPE': 'C.UTF-8',
    'XDG_CACHE_HOME': '/tmp/syndica-test-xdg',
    'UV_CACHE_DIR': '/tmp/syndica-test-uv',
    'USER': os.environ.get('USER', 'tester'),
    'LOGNAME': os.environ.get('LOGNAME', 'tester'),
    'TMPDIR': os.environ.get('TMPDIR', '/tmp'),
    **_SECRETS,
    'ZZZ_PLAIN': 'plain-but-not-listed',
    'GIT_AUTHOR_NAME': 'Syndica Bot',
}
# chat 的根固定是 Orchestrator；这里把它换成一个带 Bash 的声明式角色，三条路径才都能直接跑命令。
_CONFIG = """version: 1
workspace:
  pass_env: [GIT_AUTHOR_NAME, NOT_SET_ANYWHERE]
agents:
  - name: Orchestrator
    description: 测试用的根，自己跑命令
    system_prompt: 按要求跑命令
    tools: [Bash]
"""
# 经 Agent 的 Bash 只看环境与文件；git 怎么读这份环境在宿主一侧直接验证：Agent 的 `git config`
# 不在只读子命令里，会被开发期守卫拦下。
_PROBE = (
    'printenv; '
    'printf "\\n[gitconfig-begin]\\n"; cat "$GIT_CONFIG_GLOBAL"; printf "[gitconfig-end]\\n"; '
    'test -d "$GH_CONFIG_DIR" && printf "[gh-dir]\\n"; '
    'ls -A "$GH_CONFIG_DIR" | wc -l | tr -d " " | sed "s/^/[gh-entries]/"'
)


def _probing_llm(command: str = _PROBE) -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='probe', name='Bash', arguments=json.dumps({'command': command}))
                ]
            ),
            ScriptedTurn(content='probed'),
        ]
    )


def _host(tmp_path: Path, llm: ScriptedLLM) -> RuntimeCliHost:
    return RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path / 'ws',
        environ=_HOST_ENV,
    )


def _config(tmp_path: Path) -> str:
    (tmp_path / 'ws').mkdir(exist_ok=True)
    path = tmp_path / 'syndica.yaml'
    path.write_text(_CONFIG, encoding='utf-8')
    return str(path)


def _tool_output(llm: ScriptedLLM) -> str:
    blocks = [block for block in llm.calls[-1] if block.role == 'tool']
    assert len(blocks) == 1
    return str(blocks[0].content)


# shell 自己导出的变量：不是宿主交给 Agent 的。
_SHELL_OWN = {'PWD', 'SHLVL', '_', 'OLDPWD'}


def _env_names(output: str) -> set[str]:
    stdout = output.split('stdout:\n', 1)[1].split('\nstderr:\n', 1)[0]
    printenv = stdout.split('[gitconfig-begin]', 1)[0]
    return {line.split('=', 1)[0] for line in printenv.splitlines() if '=' in line}


def _run_probe(tmp_path: Path, llm: ScriptedLLM) -> str:
    config = _config(tmp_path)
    host = _host(tmp_path, llm)
    try:
        assert host.run_request(RunRequest(prompt='probe', config=config)) == 'probed'
    finally:
        host.close()
    return _tool_output(llm)


def test_the_agent_sees_the_whitelist_and_pass_env_but_no_secret_or_unlisted_variable(
    tmp_path: Path,
) -> None:
    output = _run_probe(tmp_path, _probing_llm())
    names = _env_names(output)

    assert {'PATH', 'HOME'} <= names
    assert 'GIT_AUTHOR_NAME=Syndica Bot' in output
    assert names.isdisjoint({*_SECRETS, 'ZZZ_PLAIN', 'NOT_SET_ANYWHERE'})
    assert not any(value in output for value in _SECRETS.values())
    assert names - _SHELL_OWN == {*_WHITELIST, 'GIT_AUTHOR_NAME', *_HOST_SET}


def test_the_agent_bash_runs_in_the_shell_this_test_run_selected(
    tmp_path: Path, shell_path: str
) -> None:
    output = _run_probe(tmp_path, _probing_llm('printf "%s" "$0"'))

    assert output.split('stdout:\n', 1)[1].split('\nstderr:\n', 1)[0] == shell_path


def test_the_whitelist_is_exactly_the_documented_one() -> None:
    assert list(FIXED_PASS_ENV) == _WHITELIST


def test_the_git_and_gh_configs_are_fresh_outside_the_workspace_and_gone_after_the_run(
    tmp_path: Path,
) -> None:
    output = _run_probe(tmp_path, _probing_llm())
    values = dict(
        line.split('=', 1) for line in output.splitlines() if line.startswith(('GIT_', 'GH_'))
    )
    git_config, gh_config = Path(values['GIT_CONFIG_GLOBAL']), Path(values['GH_CONFIG_DIR'])

    assert values['GIT_TERMINAL_PROMPT'] == '0'
    assert values['GIT_CONFIG_NOSYSTEM'] == '1'
    config_text = output.split('[gitconfig-begin]\n', 1)[1].split('[gitconfig-end]', 1)[0]
    assert config_text == '[safe]\n\tbareRepository = explicit\n'
    assert '[gh-dir]' in output
    assert '[gh-entries]0' in output
    assert not git_config.is_relative_to((tmp_path / 'ws').resolve())
    assert not git_config.exists()
    assert not gh_config.exists()
    assert not git_config.parent.exists()


def test_run_chat_and_doctor_hand_the_agent_the_same_environment(tmp_path: Path) -> None:
    run_names = _env_names(_run_probe(tmp_path, _probing_llm()))
    config = _config(tmp_path)
    chat_llm = _probing_llm()
    host = _host(tmp_path, chat_llm)
    try:
        target = host.open_chat(config)
        assert host.run_request(RunRequest(prompt='probe', session=target, config=config))
        chat_names = _env_names(_tool_output(chat_llm))
        doctor = host.doctor(config).output
    finally:
        host.close()
    doctor_line = next(line for line in doctor.splitlines() if line.startswith('env: '))

    assert chat_names == run_names
    assert set(doctor_line.removeprefix('env: ').split()) == run_names - _SHELL_OWN


def test_a_topology_run_hands_its_slot_the_same_environment_and_output_limit(
    tmp_path: Path,
) -> None:
    run_names = _env_names(_run_probe(tmp_path, _probing_llm()))
    config = _config(tmp_path)
    Path(config).write_text(
        _CONFIG + 'topologies:\n  solo:\n    slots:\n      - {name: root, agent: Orchestrator, '
        'is_root: true, cardinality: singleton, boot: eager}\n',
        encoding='utf-8',
    )
    flood = json.dumps({'command': "printf 'x%.0s' $(seq 1 30000)"})
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='probe', name='Bash', arguments=json.dumps({'command': _PROBE}))
                ]
            ),
            ScriptedTurn(tool_calls=[ToolCall(id='flood', name='Bash', arguments=flood)]),
            ScriptedTurn(content='probed'),
        ]
    )
    host = _host(tmp_path, llm)
    try:
        answer = host.run_request(RunRequest(prompt='probe', topology='solo', config=config))
    finally:
        host.close()
    blocks = {
        block.tool_call_id: str(block.content) for block in llm.calls[-1] if block.role == 'tool'
    }
    flooded = blocks['flood'].split('stdout:\n', 1)[1].split('\nstderr:\n', 1)[0]

    assert answer == 'probed'
    assert _env_names(blocks['probe']) == run_names
    assert flooded.count('x') == 10_000


def test_chat_removes_its_scratch_directory_when_the_host_closes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    llm = _probing_llm('printf "%s" "$GIT_CONFIG_GLOBAL"')
    host = _host(tmp_path, llm)
    try:
        target = host.open_chat(config)
        host.run_request(RunRequest(prompt='where', session=target, config=config))
        scratch = Path(_tool_output(llm).split('stdout:\n', 1)[1].split('\n', 1)[0]).parent
        assert scratch.is_dir()
    finally:
        host.close()

    assert not scratch.exists()


def test_run_and_doctor_leave_no_scratch_directory_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_parent = tmp_path / 'system-tmp'
    scratch_parent.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch_parent))
    config = _config(tmp_path)

    _run_probe(tmp_path, _probing_llm('true'))
    host = _host(tmp_path, _probing_llm())
    try:
        host.doctor(config)
    finally:
        host.close()

    assert list(scratch_parent.iterdir()) == []


def test_the_cli_clips_each_bash_stream_at_ten_thousand_characters(tmp_path: Path) -> None:
    output = _run_probe(tmp_path, _probing_llm("printf 'x%.0s' $(seq 1 30000)"))
    stdout = output.split('stdout:\n', 1)[1].split('\nstderr:\n', 1)[0]

    assert 'truncated: true' in output
    assert '截掉了' in stdout
    assert stdout.count('x') == 10_000


def test_chat_clips_each_bash_stream_at_ten_thousand_characters_too(tmp_path: Path) -> None:
    config = _config(tmp_path)
    llm = _probing_llm("printf 'x%.0s' $(seq 1 30000)")
    host = _host(tmp_path, llm)
    try:
        target = host.open_chat(config)
        host.run_request(RunRequest(prompt='flood', session=target, config=config))
    finally:
        host.close()
    stdout = _tool_output(llm).split('stdout:\n', 1)[1].split('\nstderr:\n', 1)[0]

    assert stdout.count('x') == 10_000


def test_a_temporary_directory_inside_the_workspace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'ws'
    (root / 'tmp').mkdir(parents=True)
    monkeypatch.setattr(tempfile, 'tempdir', str(root / 'tmp'))

    with pytest.raises(ConfigError) as caught:
        agent_environment({'PATH': '/usr/bin'}, (), workspace_root=root)

    assert 'TMPDIR' in caught.value.next_action
    assert list((root / 'tmp').iterdir()) == []


def test_git_under_the_agent_environment_reads_one_setting_and_refuses_a_planted_bare_repo(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'ws'
    planted = root / 'planted.git'
    planted.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', '--bare', str(planted)], check=True)
    environment = agent_environment(_HOST_ENV, (), workspace_root=root)
    try:
        listed = subprocess.run(
            ['git', 'config', '--show-origin', '--list'],
            cwd=root,
            env=dict(environment.values),
            capture_output=True,
            text=True,
        )
        bare = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            cwd=planted,
            env=dict(environment.values),
            capture_output=True,
            text=True,
        )
        without_the_setting = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            cwd=planted,
            env={**environment.values, 'GIT_CONFIG_GLOBAL': os.devnull},
            capture_output=True,
            text=True,
        )
        git_config = environment.values['GIT_CONFIG_GLOBAL']
    finally:
        environment.remove()

    assert listed.stdout.splitlines() == [f'file:{git_config}\tsafe.barerepository=explicit']
    assert bare.returncode == 128
    assert 'safe.bareRepository' in bare.stderr
    assert without_the_setting.returncode == 0


@pytest.mark.skipif(shutil.which('gh') is None, reason='gh 没装，占位令牌无从验证')
def test_gh_uses_the_placeholder_token_instead_of_the_users_login(tmp_path: Path) -> None:
    output = _run_probe(tmp_path, _probing_llm('gh auth token 2>/dev/null; printenv GH_TOKEN'))
    stdout = output.split('stdout:\n', 1)[1].split('\nstderr:\n', 1)[0]

    assert stdout.splitlines() == [
        'syndica-agent-has-no-github-login',
        'syndica-agent-has-no-github-login',
    ]


def test_a_temporary_directory_reached_through_a_symlink_into_the_workspace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'ws'
    (root / '.tmp').mkdir(parents=True)
    (tmp_path / 'link').symlink_to(root)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'link' / '.tmp'))

    with pytest.raises(ConfigError):
        agent_environment({'PATH': '/usr/bin'}, (), workspace_root=root)

    assert list((root / '.tmp').iterdir()) == []


def test_a_temporary_directory_spelled_in_another_case_on_a_case_blind_disk_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'CaseProbe').mkdir()
    if not (tmp_path / 'caseprobe').exists():
        pytest.skip('这块盘区分大小写，换一种大小写就是另一个目录')
    root = tmp_path / 'ws'
    (root / '.tmp').mkdir(parents=True)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'WS' / '.tmp'))

    with pytest.raises(ConfigError):
        agent_environment({'PATH': '/usr/bin'}, (), workspace_root=root)


class _BrokenStdin(StringIO):
    def readline(self, size: int | None = -1) -> str:
        raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')


def test_chat_still_cleans_up_after_reading_human_input_failed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask', name='AskUser', arguments='{"question":"name?","timeout":30}'
                    )
                ]
            ),
            ScriptedTurn(content='never'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=_BrokenStdin(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path / 'ws',
        environ=_HOST_ENV,
    )
    target = host.open_chat(config)
    with pytest.raises(UnicodeDecodeError):
        host.run_request(RunRequest(prompt='ask me', session=target, config=config))

    host.close()


def _no_model(name: str) -> ScriptedLLM:
    raise RuntimeError(f'model {name} is unavailable')


@pytest.mark.parametrize('path', ['run', 'chat', 'doctor'])
def test_a_failing_model_factory_leaves_no_scratch_behind(tmp_path: Path, path: str) -> None:
    config = _config(tmp_path)
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=_no_model,
        workspace_root=tmp_path / 'ws',
        environ=_HOST_ENV,
    )
    try:
        with pytest.raises(RuntimeError):
            if path == 'run':
                host.run_request(RunRequest(prompt='x', config=config))
            elif path == 'chat':
                host.open_chat(config)
            else:
                host.doctor(config)
    finally:
        host.close()


def test_a_missing_workspace_root_is_reported_and_leaves_no_scratch_behind(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: _probing_llm(),
        workspace_root=tmp_path / 'missing',
        environ=_HOST_ENV,
    )
    try:
        with pytest.raises(ConfigError) as caught:
            host.run_request(RunRequest(prompt='x', config=config))
    finally:
        host.close()

    assert '已存在目录' in caught.value.expected


def test_a_host_variable_set_to_the_empty_string_is_still_passed(tmp_path: Path) -> None:
    environment = agent_environment(
        {'PATH': '/usr/bin', 'NO_COLOR': ''}, ('NO_COLOR',), workspace_root=tmp_path
    )
    try:
        assert environment.values['NO_COLOR'] == ''
    finally:
        environment.remove()


def test_the_environment_takes_a_copy_of_the_host_values(tmp_path: Path) -> None:
    host = {'PATH': '/usr/bin', 'NO_COLOR': '1'}
    environment = agent_environment(host, ('NO_COLOR',), workspace_root=tmp_path)
    host['NO_COLOR'] = 'changed'
    try:
        assert environment.values['NO_COLOR'] == '1'
        assert list(environment.values)[:2] == ['PATH', 'NO_COLOR']
    finally:
        environment.remove()
