"""本仓自己的项目配置（spec D15 / D17）：`.syndica.yaml` 与 `docs/agents/*.md`。"""

from __future__ import annotations

import asyncio
import json
import re
from io import StringIO
from pathlib import Path

import pytest

from syndica.cli.config import load_config
from syndica.cli.handoff import commit_title, marker_verdict, protected_matcher
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.kernel import (
    AgentOptions,
    EventBus,
    HooksCenter,
    HostFacts,
    LocalWorkspace,
    ScriptedLLM,
    local_tools,
)

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / '.syndica.yaml'
PROMPTS = {
    'Orchestrator': 'orchestrator.md',
    'Planner': 'planner.md',
    'Explorer': 'explorer.md',
    'Worker': 'worker.md',
    'Reviewer': 'reviewer.md',
}
_GIT_BAN = '不执行 git 写命令'


def _prompt(role: str) -> str:
    return (REPO / 'docs' / 'agents' / PROMPTS[role]).read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def frozen(tmp_path_factory: pytest.TempPathFactory) -> dict[str, AgentOptions]:
    config = load_config(CONFIG)
    workspace = LocalWorkspace(str(tmp_path_factory.mktemp('ws')), shell='/bin/sh', env={})
    try:
        factories = config.catalog.freeze(
            local_tools(workspace.portal()),
            {'default': ScriptedLLM([])},
            HostFacts(os='darwin', platform='test', shell='/bin/sh'),
        )
    finally:
        asyncio.run(workspace.close())
    return {name: factories[name](HooksCenter(), EventBus()).options for name in PROMPTS}


def test_the_config_loads_and_every_role_freezes(frozen: dict[str, AgentOptions]):
    assert sorted(frozen) == sorted(PROMPTS)


@pytest.mark.parametrize('role', ['Planner', 'Explorer', 'Worker', 'Reviewer'])
def test_each_declarative_prompt_is_its_file(frozen: dict[str, AgentOptions], role: str):
    assert frozen[role].system_prompt == _prompt(role)


def test_the_orchestrator_prompt_lists_reviewer_and_ends_with_its_file(
    frozen: dict[str, AgentOptions],
):
    prompt = frozen['Orchestrator'].system_prompt

    assert prompt.endswith('\n' + _prompt('Orchestrator'))
    delegates = re.findall(r'^Delegate 给 (\w+)：', prompt, flags=re.MULTILINE)
    assert delegates == ['Planner', 'Explorer', 'Worker', 'Reviewer']


@pytest.mark.parametrize('role', sorted(PROMPTS))
def test_prompt_files_have_no_placeholders_or_unescaped_braces(role: str):
    assert re.search(r'(?<!\{)\{(?!\{)|(?<!\})\}(?!\})', _prompt(role)) is None


@pytest.mark.parametrize('role', sorted(PROMPTS))
def test_every_prompt_bans_git_writes_and_points_at_agents_md(role: str):
    text = _prompt(role)

    assert _GIT_BAN in text
    assert '`AGENTS.md`' in text


@pytest.mark.parametrize(
    ('role', 'anchor'),
    [
        ('Worker', '`^##`'),
        ('Worker', '按行读'),
        ('Worker', '只读它开头的表格'),
        ('Worker', '不要整篇读'),
        ('Worker', '不要为了验证而故意改坏代码'),
        ('Worker', '也不要 `git add`'),
        ('Worker', '每次改动后跑最小相关的测试'),
        ('Worker', '--tb=short'),
        ('Worker', 'timeout 参数（全量测试给 300 秒）'),
        ('Worker', '不要放到后台跑'),
        ('Worker', 'cwd 参数'),
        ('Worker', 'POSIX sh'),
        ('Worker', 'dash'),
        ('Worker', '以 `/` 开头的 sed / awk 脚本'),
        ('Worker', '写 `/tmp` 会被拦下'),
        ('Worker', '`.syndica/tmp/`'),
        ('Reviewer', '`^##`'),
        ('Reviewer', '按行读'),
        ('Reviewer', '不要整篇读'),
        ('Reviewer', '`git diff --stat`'),
        ('Reviewer', '`git diff -- <路径>`'),
        ('Reviewer', '`uv run ruff format --check .`'),
        ('Reviewer', '不要真的去改代码验证'),
        ('Reviewer', '不要放到后台跑'),
        ('Reviewer', '不带 `--check` 的 `ruff format`'),
        ('Reviewer', 'POSIX sh'),
        ('Reviewer', '不用 `cd ..` 回去'),
        ('Reviewer', '以 `/` 开头的 sed / awk 脚本'),
        ('Reviewer', '也不派活'),
        ('Reviewer', '只有 pass 会让宿主提供提交选项'),
        ('Reviewer', 'partial：有问题但不严重。宿主不会提供提交选项'),
        ('Orchestrator', '一定再派一次 Reviewer'),
        ('Orchestrator', '每次评审都派一个新的 Reviewer'),
        ('Orchestrator', 'block 或 partial'),
        ('Orchestrator', '另派一个新的 Worker'),
        ('Orchestrator', '改完再派一个新的 Reviewer'),
        ('Orchestrator', '只写一行'),
        ('Planner', '不要整篇读'),
        ('Planner', '也不派活'),
        ('Explorer', '不改任何文件'),
        ('Explorer', '也不派活'),
    ],
)
def test_each_d17_prompt_requirement_is_stated(role: str, anchor: str):
    assert anchor in _prompt(role)


@pytest.mark.parametrize('role', sorted(PROMPTS))
def test_git_branch_is_only_ever_mentioned_as_blocked(role: str):
    lines = [line for line in _prompt(role).splitlines() if 'git branch' in line]

    assert all('拦下' in line for line in lines)


@pytest.mark.parametrize('role', sorted(PROMPTS))
def test_prompt_files_have_no_byte_order_mark(role: str):
    assert not _prompt(role).startswith('\ufeff')


def test_the_agents_md_sections_the_prompts_cite_exist():
    headings = [
        line
        for line in (REPO / 'AGENTS.md').read_text(encoding='utf-8').splitlines()
        if line.startswith('##')
    ]

    for section in ('硬约束', '错误消息', '测试'):
        assert any(line.lstrip('#').strip().startswith(section) for line in headings), section
        for role in ('Worker', 'Reviewer'):
            assert f'「{section}」' in _prompt(role)


def test_the_worker_prompt_lists_every_verify_gate():
    for command in load_config(CONFIG).workspace.verify:
        assert f'`{command}`' in _prompt('Worker'), command


def test_the_reviewer_prompt_shows_a_verdict_line_the_grammar_accepts():
    assert marker_verdict(_prompt('Reviewer')) == 'block'


def test_the_orchestrator_prompt_shows_a_commit_title_line_the_grammar_accepts():
    assert commit_title(_prompt('Orchestrator')) == '<一句话说清这次改了什么>'


def test_role_turn_budgets(frozen: dict[str, AgentOptions]):
    budgets = {role: options.max_turns for role, options in frozen.items()}

    assert budgets == {
        'Orchestrator': 24,
        'Planner': 8,
        'Explorer': 10,
        'Worker': 24,
        'Reviewer': 10,
    }


def test_each_role_has_exactly_its_tools(frozen: dict[str, AgentOptions]):
    tools = {role: sorted(options.tools) for role, options in frozen.items()}

    assert tools == {
        'Orchestrator': [],
        'Planner': ['Glob', 'Grep', 'Read'],
        'Explorer': ['Glob', 'Grep', 'Read'],
        'Worker': ['Bash', 'BashOutput', 'Edit', 'Glob', 'Grep', 'KillBash', 'Read', 'Write'],
        'Reviewer': ['Bash', 'Glob', 'Grep', 'Read'],
    }


def test_the_topology_is_exactly_one_root_singleton_orchestrator_slot():
    stdout = StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO())
    host = RuntimeCliHost(io=io, model_factory=lambda _: ScriptedLLM([]), workspace_root=REPO)
    try:
        code = main(['--config', str(CONFIG), 'topology', 'show', 'syndica-dev'], host=host, io=io)
    finally:
        host.close()

    assert code == 0
    assert json.loads(stdout.getvalue())['slots'] == [
        {
            'name': 'root',
            'agent': 'Orchestrator',
            'is_root': True,
            'cardinality': 'singleton',
            'key_field': None,
            'boot': 'lazy',
            'exit_mode': 'zombie',
            'extra_tools': [],
            'budget': None,
            'metadata': {},
        }
    ]


def test_relative_config_paths_belong_to_the_workspace_not_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(REPO)
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'team.yaml').write_text(
        'version: 1\nagents:\n  - name: Solo\n    description: alone\n    system_prompt: hi\n',
        encoding='utf-8',
    )

    def listed(*argv: str) -> str:
        stdout = StringIO()
        io = CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO())
        host = RuntimeCliHost(
            io=io, model_factory=lambda _: ScriptedLLM([]), workspace_root=workspace
        )
        try:
            assert main([*argv, 'agents', 'list'], host=host, io=io) == 0
        finally:
            host.close()
        return stdout.getvalue()

    assert 'Reviewer' not in listed().split()
    assert 'Solo' in listed('--config', 'team.yaml').split()

    (workspace / '.syndica.yaml').write_text('version: 1\n', encoding='utf-8')
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=workspace,
    )
    try:
        doctor = host.doctor('.syndica.yaml').output
    finally:
        host.close()
    assert f'config: ok {workspace / ".syndica.yaml"}' in doctor.splitlines()


def test_verify_is_exactly_the_ci_gates():
    workflow = (REPO / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
    gates = [
        line.split('- run: ', 1)[1].strip()
        for line in workflow.splitlines()
        if line.strip().startswith('- run: uv run ')
    ]

    assert len(gates) == 6
    assert list(load_config(CONFIG).workspace.verify) == gates


@pytest.mark.parametrize(
    'path',
    [
        'pyproject.toml',
        'uv.lock',
        'conftest.py',
        'tests/cli/conftest.py',
        'pytest.ini',
        '.pytest.ini',
        'pyrightconfig.json',
        'ruff.toml',
        'src/ruff.toml',
        '.ruff.toml',
        'setup.cfg',
        'tox.ini',
        'scripts/check_deps.py',
        'scripts/lib/util.py',
        '.syndica.yaml',
        'docs/agents/worker.md',
        'docs/agents/extra/notes.txt',
        '.github/workflows/ci.yml',
        '.github/actions/setup/action.yml',
        'src/.ruff.toml',
        'uv.toml',
        '.python-version',
        'AGENTS.md',
        '.gitignore',
        'src/.gitattributes',
    ],
)
def test_the_files_that_define_passing_are_protected(path: str):
    assert protected_matcher(load_config(CONFIG).workspace.protected)(path)


@pytest.mark.parametrize(
    'path', ['src/syndica/cli/host.py', 'tests/cli/test_main.py', 'docs/plans/x.md', 'README.md']
)
def test_ordinary_source_and_docs_are_not_protected(path: str):
    assert not protected_matcher(load_config(CONFIG).workspace.protected)(path)
