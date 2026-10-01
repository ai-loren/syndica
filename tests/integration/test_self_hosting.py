"""自举场景（M2 计划 T17）：经公开 CLI、用按角色分发的脚本化 LLM，在夹具项目的 tmp 副本里跑。

夹具在 `tests/fixtures/self_hosting/`，每个文件带 `.fixture` 后缀（pytest、ruff、pyright 与链接检查
都不把它当成仓库内容），还原时去掉。每个场景结束时夹具源目录的 sha256 不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.kernel import ContextBlock, LLMConfig, ScriptedLLM, ScriptedTurn, ToolCall

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests' / 'fixtures' / 'self_hosting'
PYTHON = shlex.quote(sys.executable)
PYTEST = f'{PYTHON} -m pytest -q -p no:cacheprovider'


def _digest(root: Path, *, skip: frozenset[str] = frozenset()) -> str:
    """目录树的 sha256：相对路径、文件类型与内容都算；`skip` 里的顶层名字不算。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if relative.parts[0] in skip:
            continue
        digest.update(relative.as_posix().encode('utf-8', 'surrogateescape') + b'\0')
        if path.is_symlink():
            digest.update(b'link:' + os.readlink(path).encode('utf-8', 'surrogateescape'))
        elif path.is_file():
            digest.update(b'file:' + path.read_bytes())
        else:
            digest.update(b'dir')
        digest.update(b'\0')
    return digest.hexdigest()


@pytest.fixture(autouse=True)
def fixture_source_unchanged() -> Iterator[None]:
    before = _digest(FIXTURE)
    yield
    assert _digest(FIXTURE) == before


@pytest.fixture(autouse=True)
def no_agent_scratch_left_behind(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    scratch_parent = tmp_path_factory.mktemp('system-tmp')
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch_parent))
    yield
    assert sorted(path.name for path in scratch_parent.glob('syndica-agent-*')) == []


def _env(tmp_path: Path) -> dict[str, str]:
    """git 按 T12 隔离：本地身份、空的全局 hooksPath（git 模板目录里的钩子进不来）。"""
    home = tmp_path / 'home'
    (home / 'hooks').mkdir(parents=True, exist_ok=True)
    config = tmp_path / 'global.gitconfig'
    if not config.exists():
        config.write_text(
            '[user]\n\tname = Syndica Test\n\temail = test@example.com\n'
            f'[core]\n\thooksPath = {home / "hooks"}\n[init]\n\tdefaultBranch = main\n',
            encoding='utf-8',
        )
    environ = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return {
        **environ,
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': str(config),
        'GIT_CONFIG_NOSYSTEM': '1',
    }


def _git(root: Path, tmp_path: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=root, env=_env(tmp_path), check=True, capture_output=True, text=True
    ).stdout


def _project(tmp_path: Path, extra: Mapping[str, str] | None = None) -> Path:
    """把夹具还原进 tmp（去掉 `.fixture` 后缀），连同 `extra` 一起提交成一个 git 仓库。"""
    root = tmp_path / 'project'
    for source in sorted(FIXTURE.rglob('*.fixture')):
        target = root / source.relative_to(FIXTURE).with_suffix('')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for name, content in (extra or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding='utf-8')
    _git(tmp_path, tmp_path, 'init', '-q', '-b', 'main', str(root))
    _git(root, tmp_path, 'add', '.')
    _git(root, tmp_path, 'commit', '-q', '-m', 'fixture')
    return root


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _bash(call_id: str, command: str) -> ToolCall:
    return _call(call_id, 'Bash', command=command)


class _ByRole:
    """按 System Prompt 认角色、按角色回放脚本的假 LLM。派活是串行的，所以每个角色的调用顺序
    确定。"""

    def __init__(self, scripts: Mapping[str, list[ScriptedTurn]], markers: Mapping[str, str]):
        self.llms = {role: ScriptedLLM(turns) for role, turns in scripts.items()}
        self._markers = dict(markers)

    @property
    def config(self) -> LLMConfig:
        return next(iter(self.llms.values())).config

    def cache_prefix_units(self, messages: list[ContextBlock], **kwargs: Any) -> tuple[bytes, ...]:
        return self._for(messages).cache_prefix_units(messages, **kwargs)

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        return await self._for(messages).create(messages, **kwargs)

    def _for(self, messages: list[ContextBlock]) -> ScriptedLLM:
        system = str(messages[0].content)
        roles = [role for role, marker in self._markers.items() if marker in system]
        assert len(roles) == 1, f'认不出角色：{roles!r}，System Prompt 开头是 {system[:60]!r}'
        return self.llms[roles[0]]

    def tool_results(self) -> dict[str, str]:
        """所有角色收到的工具结果，按调用 id。"""
        found: dict[str, str] = {}
        for llm in self.llms.values():
            for call in llm.calls:
                for block in call:
                    if block.role == 'tool' and block.tool_call_id is not None:
                        found[block.tool_call_id] = str(block.content)
        return found


class _Run:
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr

    def __repr__(self) -> str:
        # 断言失败时 pytest 打印这里：认不出角色之类的原因只在 CLI 的输出里。
        stdout, stderr = self.stdout[-600:], self.stderr[-600:]
        return f'_Run(code={self.code}, stdout=…{stdout!r}, stderr=…{stderr!r})'

    @property
    def matter(self) -> dict[str, str]:
        head = self.stdout.split('---\n')[1]
        return dict(line.split(': ', 1) for line in head.splitlines())


def _cli(tmp_path: Path, llm: Any, root: Path, argv: list[str], *, stdin: str = '') -> _Run:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        io=io, model_factory=lambda _: llm, workspace_root=root, environ=_env(tmp_path)
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close()
    return _Run(code, stdout.getvalue(), stderr.getvalue())


def _team_config(tmp_path: Path, verify: list[str]) -> Path:
    """Lead 改文件、派 Reviewer：小场景用的配置，放在工作区之外。"""
    (tmp_path / 'lead.md').write_text('You lead.\n', encoding='utf-8')
    (tmp_path / 'reviewer.md').write_text('You review.\n', encoding='utf-8')
    config = {
        'version': 1,
        'workspace': {'verify': verify},
        'agents': [
            {
                'name': 'Reviewer',
                'description': 'reviews',
                'system_prompt': 'reviewer.md',
                'tools': ['Read', 'Bash'],
            },
            {
                'name': 'Lead',
                'description': 'leads',
                'system_prompt': 'lead.md',
                'tools': ['Read', 'Write', 'Edit', 'Bash'],
                'subagents': ['Reviewer'],
            },
        ],
    }
    path = tmp_path / 'team.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    return path


_TEAM = {'Lead': 'You lead.', 'Reviewer': 'You review.'}


def _review(verdict: str = 'pass') -> list[ScriptedTurn]:
    return [
        ScriptedTurn(tool_calls=[_bash('r1', 'git diff')]),
        ScriptedTurn(content=f'改动看过了。\nverdict: {verdict}'),
    ]


def _fix_calc(prefix: str = 'w') -> list[ScriptedTurn]:
    return [
        ScriptedTurn(
            tool_calls=[
                _call(
                    f'{prefix}1',
                    'Edit',
                    path='pkg/calc.py',
                    old_string='return a - b',
                    new_string='return a + b',
                )
            ]
        )
    ]


def _remote(tmp_path: Path, root: Path) -> Path:
    """本地 bare 仓库作上游，当前的 main 已经推上去。"""
    remote = tmp_path / 'remote.git'
    _git(tmp_path, tmp_path, 'init', '-q', '--bare', str(remote))
    _git(root, tmp_path, 'remote', 'add', 'origin', str(remote))
    _git(root, tmp_path, 'push', '-q', '-u', 'origin', 'main')
    return remote


def _fixed_and_reviewed(reviewer: list[ScriptedTurn] | None = None) -> _ByRole:
    """Lead 修好 add、派一个 Reviewer，最后给出提交标题。"""
    return _ByRole(
        {
            'Lead': [
                *_fix_calc(),
                ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='审')]),
                ScriptedTurn(content='修好了。\ncommit_title: 修好 calc.add'),
            ],
            'Reviewer': _review() if reviewer is None else reviewer,
        },
        _TEAM,
    )


def _lead_handoff(
    tmp_path: Path, llm: _ByRole, root: Path, verify: list[str], *, stdin: str = ''
) -> _Run:
    argv = ['--config', str(_team_config(tmp_path, verify)), 'run', '--handoff']
    return _cli(tmp_path, llm, root, [*argv, '--agent', 'Lead', '修好 add'], stdin=stdin)


# ---- 1. 失败迭代：本仓的 syndica-dev 角色与拓扑 ----


def test_a_failed_attempt_is_fixed_and_reviewed_with_the_repos_own_roles(tmp_path: Path):
    config = yaml.safe_load((REPO / '.syndica.yaml').read_text(encoding='utf-8'))
    config['workspace']['verify'] = [PYTEST]
    extra = {
        '.syndica.yaml': yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        **{
            f'docs/agents/{path.name}': path.read_text(encoding='utf-8')
            for path in sorted((REPO / 'docs' / 'agents').glob('*.md'))
        },
    }
    root = _project(tmp_path, extra)
    llm = _ByRole(
        {
            'Orchestrator': [
                ScriptedTurn(
                    tool_calls=[_call('o1', 'Delegate', agent='Worker', query='修好 add')]
                ),
                ScriptedTurn(
                    tool_calls=[_call('o2', 'Delegate', agent='Reviewer', query='审改动')]
                ),
                ScriptedTurn(content='add 修好了，测试通过。\ncommit_title: 修好 calc.add'),
            ],
            'Worker': [
                # 错的那一版与改对的那一版长度不同：两次改动常在同一秒内完成，而 .pyc 按（mtime 秒,
                # 字节数）判失效，等长改动会让第二次 pytest 用上第一次的字节码。
                ScriptedTurn(
                    tool_calls=[
                        _call(
                            'w1',
                            'Edit',
                            path='pkg/calc.py',
                            old_string='return a - b',
                            new_string='return a * b + 0',
                        )
                    ]
                ),
                ScriptedTurn(tool_calls=[_bash('w2', PYTEST)]),
                ScriptedTurn(
                    tool_calls=[
                        _call(
                            'w3',
                            'Edit',
                            path='pkg/calc.py',
                            old_string='return a * b + 0',
                            new_string='return a + b',
                        )
                    ]
                ),
                ScriptedTurn(tool_calls=[_bash('w4', PYTEST)]),
                ScriptedTurn(content='第一次改错了，第二次改对，测试通过。'),
            ],
            'Reviewer': _review(),
        },
        {
            'Orchestrator': '你是 Syndica 仓库的编排者',
            'Worker': '你是 Syndica 仓库的执行者',
            'Reviewer': '你是 Syndica 仓库的评审者',
        },
    )

    run = _cli(
        tmp_path,
        llm,
        root,
        ['run', '--handoff', '--topology', 'syndica-dev', '修好 pkg/calc.py'],
    )

    assert run.code == 0
    assert run.matter['status'] == 'verified'
    assert (run.matter['host_verify'], run.matter['reviewer_verdict']) == ('passed', 'pass')
    assert '- ` M` pkg/calc.py' in run.stdout.splitlines()
    checks = [line for line in run.stdout.splitlines() if line.startswith(f'- `{PYTEST}`')]
    assert [line.split('：')[1][:6] for line in checks] == ['failed', 'passed']
    results = llm.tool_results()
    assert '1 failed' in results['w2'] and '1 passed' in results['w4']


def test_a_pass_given_before_the_last_change_does_not_count(tmp_path: Path):
    root = _project(tmp_path)
    llm = _ByRole(
        {
            'Lead': [
                ScriptedTurn(
                    tool_calls=[
                        _call(
                            'w1',
                            'Edit',
                            path='pkg/calc.py',
                            old_string='return a - b',
                            new_string='return a * b + 0',
                        )
                    ]
                ),
                ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='审')]),
                ScriptedTurn(
                    tool_calls=[
                        _call(
                            'w2',
                            'Edit',
                            path='pkg/calc.py',
                            old_string='return a * b + 0',
                            new_string='return a + b',
                        )
                    ]
                ),
                ScriptedTurn(content='修好了。\ncommit_title: 修好 calc.add'),
            ],
            'Reviewer': _review(),
        },
        _TEAM,
    )

    run = _lead_handoff(tmp_path, llm, root, [PYTEST], stdin='1\n')

    assert (run.matter['status'], run.matter['host_verify']) == ('unverified', 'passed')
    assert (run.matter['reviewer_verdict'], run.matter['git']) == ('unverified', 'not_offered')


def test_a_reviewer_that_wrote_through_bash_does_not_count(tmp_path: Path):
    root = _project(tmp_path)
    reviewer = [
        ScriptedTurn(tool_calls=[_bash('r1', "printf 'ok\\n' > review-notes.txt")]),
        ScriptedTurn(content='看过了。\nverdict: pass'),
    ]

    run = _lead_handoff(tmp_path, _fixed_and_reviewed(reviewer), root, [PYTEST], stdin='1\n')

    assert (run.matter['status'], run.matter['reviewer_verdict']) == ('unverified', 'unverified')
    assert run.matter['git'] == 'not_offered'


def test_the_later_of_two_reviews_counts(tmp_path: Path):
    root = _project(tmp_path)
    llm = _ByRole(
        {
            'Lead': [
                *_fix_calc(),
                ScriptedTurn(tool_calls=[_call('d1', 'Delegate', agent='Reviewer', query='审')]),
                ScriptedTurn(tool_calls=[_call('d2', 'Delegate', agent='Reviewer', query='再审')]),
                ScriptedTurn(content='修好了。'),
            ],
            'Reviewer': [
                ScriptedTurn(tool_calls=[_bash('r1', 'git diff')]),
                ScriptedTurn(content='先挡一下。\nverdict: block'),
                ScriptedTurn(tool_calls=[_bash('r2', 'git diff')]),
                ScriptedTurn(content='没问题了。\nverdict: pass'),
            ],
        },
        _TEAM,
    )

    run = _lead_handoff(tmp_path, llm, root, [PYTEST])

    assert (run.matter['status'], run.matter['reviewer_verdict']) == ('verified', 'pass')


# ---- 2. 安全拦截 ----


def test_dangerous_commands_are_blocked_and_the_agent_takes_another_way(tmp_path: Path):
    root = _project(tmp_path, {'build/old.txt': 'stale\n'})
    llm = _ByRole(
        {
            'Lead': [
                ScriptedTurn(tool_calls=[_bash('push', 'git push')]),
                ScriptedTurn(tool_calls=[_bash('setsid', 'setsid git push')]),
                ScriptedTurn(tool_calls=[_bash('rmrf', 'rm -rf build')]),
                ScriptedTurn(tool_calls=[_bash('rm', 'rm build/old.txt')]),
                ScriptedTurn(content='推送不归我管；build 里的旧文件删掉了。'),
            ]
        },
        _TEAM,
    )

    run = _cli(
        tmp_path,
        llm,
        root,
        ['--config', str(_team_config(tmp_path, [])), 'run', '--agent', 'Lead', '清理 build'],
    )

    results = llm.tool_results()
    assert run.code == 0
    assert results['push'].startswith('已拦截（shell.git_state_change）')
    assert results['setsid'].startswith('已拦截（shell.git_state_change）')
    assert 'shell.recursive_force_remove' in results['rmrf']
    assert '本次调用已被阻断，没有执行' in results['rmrf']
    assert 'exit_code: 0' in results['rm']
    assert (root / 'build').is_dir() and not (root / 'build' / 'old.txt').exists()


def test_a_blocked_git_write_leaves_later_confirmations_with_the_human(tmp_path: Path):
    root = _project(tmp_path)
    llm = _ByRole(
        {
            'Lead': [
                ScriptedTurn(tool_calls=[_bash('push', 'git push')]),
                ScriptedTurn(tool_calls=[_bash('hosts', 'cat /etc/hosts')]),
                ScriptedTurn(content='推送不归我管；hosts 读到了。'),
            ]
        },
        _TEAM,
    )

    run = _cli(
        tmp_path,
        llm,
        root,
        ['--config', str(_team_config(tmp_path, [])), 'run', '--agent', 'Lead', '看看 hosts'],
        stdin='approve\n',
    )

    results = llm.tool_results()
    assert results['push'].startswith('已拦截（shell.git_state_change）')
    assert run.stdout.count('Approve Bash') == 1
    assert 'exit_code: 0' in results['hosts']


# ---- 3. 人工确认 ----


@pytest.mark.parametrize('answers', ['approve\nreject\n', 'approve\n'], ids=['reject', 'eof'])
def test_one_of_two_confirmed_reads_is_approved_and_only_that_one_runs(
    tmp_path: Path, answers: str
):
    root = _project(tmp_path)
    llm = _ByRole(
        {
            'Lead': [
                ScriptedTurn(
                    tool_calls=[
                        _bash('first', 'cat /etc/hosts > first.txt'),
                        _bash('second', 'cat /etc/hosts > second.txt'),
                    ]
                ),
                ScriptedTurn(content='读到了一次。'),
            ]
        },
        _TEAM,
    )

    run = _cli(
        tmp_path,
        llm,
        root,
        ['--config', str(_team_config(tmp_path, [])), 'run', '--agent', 'Lead', '看看 hosts'],
        stdin=answers,
    )

    results = llm.tool_results()
    assert run.code == 0
    assert run.stdout.count('Approve Bash') == 2
    assert 'exit_code: 0' in results['first']
    assert 'exit_code: 0' not in results['second']
    assert (root / 'first.txt').is_file()
    assert not (root / 'second.txt').exists()


# ---- 4. 只读 ----


def test_a_read_only_run_cannot_write_and_leaves_the_workspace_untouched(tmp_path: Path):
    root = _project(tmp_path)
    before = _digest(root, skip=frozenset({'.syndica'}))
    llm = _ByRole(
        {
            'Lead': [
                ScriptedTurn(
                    tool_calls=[_call('w', 'Write', path='pkg/new.py', content='NEW = 1\n')]
                ),
                ScriptedTurn(tool_calls=[_bash('b', "printf 'x\\n' > leak.txt")]),
                ScriptedTurn(content='只能读，没有改。'),
            ]
        },
        _TEAM,
    )

    run = _cli(
        tmp_path,
        llm,
        root,
        [
            '--config',
            str(_team_config(tmp_path, [PYTEST])),
            'run',
            '--handoff',
            '--no-write',
            '--agent',
            'Lead',
            '规划一个改动',
        ],
    )

    results = llm.tool_results()
    assert run.code == 0
    assert run.matter['status'] == 'read_only'
    assert (run.matter['host_verify'], run.matter['git']) == ('skipped', 'not_offered')
    assert "工具名是已注册工具之一。 实际: 收到 'Write'" in results['w']
    assert "工具名是已注册工具之一。 实际: 收到 'Bash'" in results['b']
    assert _digest(root, skip=frozenset({'.syndica'})) == before


# ---- 5. 文档自举 ----


def test_a_broken_link_is_fixed_and_the_link_check_is_listed(tmp_path: Path):
    root = _project(
        tmp_path, {'scripts/check_links.py': (REPO / 'scripts' / 'check_links.py').read_text()}
    )
    check = f'{PYTHON} scripts/check_links.py'
    llm = _ByRole(
        {
            'Lead': [
                ScriptedTurn(tool_calls=[_bash('l0', check)]),
                ScriptedTurn(
                    tool_calls=[
                        _call(
                            'l1',
                            'Edit',
                            path='docs/guide.md',
                            old_string='(setup.md)',
                            new_string='(install.md)',
                        )
                    ]
                ),
                ScriptedTurn(tool_calls=[_bash('l2', check)]),
                ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='审')]),
                ScriptedTurn(content='断链修好了。'),
            ],
            'Reviewer': _review(),
        },
        _TEAM,
    )

    run = _cli(
        tmp_path,
        llm,
        root,
        [
            '--config',
            str(_team_config(tmp_path, [check])),
            'run',
            '--handoff',
            '--agent',
            'Lead',
            '修好文档里的断链',
        ],
    )

    assert run.code == 0
    assert run.matter['status'] == 'verified'
    assert '- ` M` docs/guide.md' in run.stdout.splitlines()
    checks = [line for line in run.stdout.splitlines() if line.startswith(f'- `{check}`')]
    assert [line.split('：')[1][:6] for line in checks] == ['failed', 'passed']


# ---- 6. 不伪装成功 ----


def test_an_agent_claiming_success_while_verify_fails_is_not_believed(tmp_path: Path):
    root = _project(tmp_path)
    llm = _ByRole(
        {
            'Lead': [
                ScriptedTurn(
                    tool_calls=[_call('w', 'Write', path='notes.txt', content='looked at it\n')]
                ),
                ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='审')]),
                ScriptedTurn(content='全部完成，测试都通过了。'),
            ],
            'Reviewer': _review(),
        },
        _TEAM,
    )

    run = _cli(
        tmp_path,
        llm,
        root,
        [
            '--config',
            str(_team_config(tmp_path, [PYTEST])),
            'run',
            '--handoff',
            '--agent',
            'Lead',
            '修好 add',
        ],
        stdin='1\n',
    )

    assert run.code == 1
    assert (run.matter['status'], run.matter['host_verify']) == ('verification_failed', 'failed')
    assert run.matter['git'] == 'not_offered'
    assert '选哪个' not in run.stderr


# ---- 7. git 收尾 ----


def test_answering_1_pushes_one_commit_with_only_this_runs_change(tmp_path: Path):
    root = _project(tmp_path)
    remote = _remote(tmp_path, root)
    pushed_before = _git(remote, tmp_path, 'rev-parse', 'main').strip()

    run = _lead_handoff(tmp_path, _fixed_and_reviewed(), root, [PYTEST], stdin='1\n')

    assert run.code == 0
    assert (run.matter['status'], run.matter['git']) == ('verified', 'pushed')
    assert _git(remote, tmp_path, 'rev-parse', 'main^').strip() == pushed_before
    names = _git(remote, tmp_path, 'diff-tree', '-r', '--name-only', '--no-commit-id', 'main')
    assert names.split() == ['pkg/calc.py']
    assert _git(remote, tmp_path, 'log', '-1', '--format=%s', 'main').strip() == '修好 calc.add'


def test_answering_3_leaves_history_and_the_worktree_as_they_were(tmp_path: Path):
    root = _project(tmp_path)
    remote = _remote(tmp_path, root)
    pushed_before = _git(remote, tmp_path, 'rev-parse', 'main').strip()

    run = _lead_handoff(tmp_path, _fixed_and_reviewed(), root, [PYTEST], stdin='3\n')

    assert run.code == 0
    assert (run.matter['status'], run.matter['git']) == ('verified', 'declined')
    assert _git(remote, tmp_path, 'rev-parse', 'main').strip() == pushed_before
    assert _git(root, tmp_path, 'rev-parse', 'HEAD').strip() == pushed_before
    assert _git(root, tmp_path, 'status', '--porcelain').splitlines() == [' M pkg/calc.py']


def test_a_commit_leaves_the_humans_own_edit_out(tmp_path: Path):
    root = _project(tmp_path)
    (root / 'README.md').write_text('# calc\n\nwork in progress\n', encoding='utf-8')

    run = _lead_handoff(tmp_path, _fixed_and_reviewed(), root, [PYTEST], stdin='2\n')

    assert run.matter['git'] == 'committed'
    names = _git(root, tmp_path, 'diff-tree', '-r', '--name-only', '--no-commit-id', 'HEAD')
    assert names.split() == ['pkg/calc.py']
    assert _git(root, tmp_path, 'status', '--porcelain').splitlines() == [' M README.md']


def test_no_push_is_offered_while_local_commits_are_ahead_of_the_upstream(tmp_path: Path):
    root = _project(tmp_path)
    remote = _remote(tmp_path, root)
    pushed_before = _git(remote, tmp_path, 'rev-parse', 'main').strip()
    (root / 'NOTES.md').write_text('local only\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'NOTES.md')
    _git(root, tmp_path, 'commit', '-q', '-m', 'not pushed yet')

    run = _lead_handoff(tmp_path, _fixed_and_reviewed(), root, [PYTEST], stdin='1\n')

    assert '1. 提交并推送' not in run.stderr
    assert run.matter['git'] == 'declined'
    assert _git(remote, tmp_path, 'rev-parse', 'main').strip() == pushed_before


def test_every_fixture_file_carries_the_fixture_suffix():
    found = [Path(folder, name) for folder, _, names in os.walk(FIXTURE) for name in names]

    assert len(found) == 7
    assert [path for path in found if path.suffix != '.fixture'] == []
