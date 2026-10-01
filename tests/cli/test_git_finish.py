"""git 收尾（spec D14）：经 CLI 宿主跑脚本化的 run，看询问、提交、推送、交接文件与退出码。

git 全部隔离：`HOME` 与 `GIT_CONFIG_GLOBAL` 指向临时目录里的配置（本地身份、全局 hooksPath），
`GIT_CONFIG_NOSYSTEM=1`；remote 是本地 bare 仓库。
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import shutil
import signal
import string
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from typing import Any, TextIO, cast

import pytest

import syndica.cli.git_finish as git_finish
import syndica.cli.host as host_module
from syndica.cli.git_finish import (
    Integrity,
    Offer,
    ask,
    commit_message,
    integrity,
    line_counts,
    offer,
    summary,
    verification_gaps,
)
from syndica.cli.host import RuntimeCliHost
from syndica.cli.host_git import Change, HostGit
from syndica.cli.main import CliIO, main
from syndica.kernel import ContextBlock, RuleRedactor, ScriptedLLM, ScriptedTurn, ToolCall

_SECRET = 'Zq9' + 'Lm4Xw7Pk2Rt8Vb5Nc3Jd6'
_MESSAGE = 'add new\n\n宿主复核：\n- 通过：true —— passed\n'
# 读钩子写下的 pid / 标记的用例里的提交超时：钩子得在超时之前跑到写文件那一行，否则断言读不到它。
# 机器忙时提交本身就能用掉两秒。
_HOOK_STARTS_WITHIN = 5.0


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _write(path: str, content: str = 'NEW = 1\n', call_id: str = 'w') -> ToolCall:
    return _call(call_id, 'Write', path=path, content=content)


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / 'home'
    (home / 'hooks').mkdir(parents=True, exist_ok=True)
    config = _global_config(tmp_path)
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
        'SERVICE_TOKEN': _SECRET,
        'SSH_AUTH_SOCK': str(tmp_path / 'agent.sock'),
    }


def _global_config(tmp_path: Path) -> Path:
    """不放在 `$HOME/.gitconfig`：那是 git 的默认位置，放在那里就看不出提交用的是不是宿主的配置。"""
    return tmp_path / 'global.gitconfig'


def _git(root: Path, tmp_path: Path, *args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=root, env=_env(tmp_path), check=True, capture_output=True, text=True
    ).stdout


def _repository(tmp_path: Path, *, upstream: bool = True) -> Path:
    root = tmp_path / 'ws'
    (root / 'src').mkdir(parents=True)
    (root / 'README.md').write_text('# project\n', encoding='utf-8')
    (root / 'src' / 'keep.py').write_text('KEEP = 1\n', encoding='utf-8')
    (root / '.gitignore').write_text('.env\n', encoding='utf-8')
    _git(tmp_path, tmp_path, 'init', '-q', '-b', 'main', str(root))
    _git(root, tmp_path, 'add', '.')
    _git(root, tmp_path, 'commit', '-q', '-m', 'init')
    if upstream:
        _git(tmp_path, tmp_path, 'init', '-q', '--bare', str(tmp_path / 'remote.git'))
        _git(root, tmp_path, 'remote', 'add', 'origin', str(tmp_path / 'remote.git'))
        _git(root, tmp_path, 'push', '-q', '-u', 'origin', 'main')
    return root


def _with_tracked(tmp_path: Path, files: dict[str, str]) -> Path:
    root = _repository(tmp_path)
    for name, content in files.items():
        (root / name).write_text(content, encoding='utf-8')
    _git(root, tmp_path, 'add', '.')
    _git(root, tmp_path, 'commit', '-q', '-m', 'more')
    _git(root, tmp_path, 'push', '-q')
    return root


def _hook(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / 'home' / 'hooks' / name
    path.write_text(f'#!/bin/sh\n{body}\n', encoding='utf-8')
    path.chmod(0o755)
    return path


def _github_token() -> str:
    rng = random.Random(1234)
    return 'ghp_' + ''.join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def _config(tmp_path: Path, verify: Sequence[str] | None, protected: list[str] | None) -> Path:
    (tmp_path / 'lead.md').write_text('You lead.\n', encoding='utf-8')
    (tmp_path / 'reviewer.md').write_text('You review.\n', encoding='utf-8')
    workspace: dict[str, object] = {}
    if verify is not None:
        workspace['verify'] = list(verify)
    if protected is not None:
        workspace['protected'] = protected
    lines = [
        'version: 1',
        'agents:',
        '  - name: Reviewer',
        '    description: reviews',
        '    system_prompt: reviewer.md',
        '    tools: [Read, Bash]',
        '  - name: Lead',
        '    description: leads',
        '    system_prompt: lead.md',
        '    tools: [Write, Edit, Bash]',
        '    subagents: [Reviewer]',
    ]
    if workspace:
        lines.append(f'workspace: {json.dumps(workspace)}')
    path = tmp_path / 'syndica.yaml'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def _script(
    *calls: ToolCall, verdict: str | None = 'pass', answer: str = 'done\ncommit_title: add new'
) -> list[ScriptedTurn]:
    """Lead 改文件、派 Reviewer；Reviewer 给出结论；Lead 收尾。"""
    review = 'looks fine' + (f'\nverdict: {verdict}' if verdict is not None else '')
    return [
        ScriptedTurn(tool_calls=list(calls or (_write('src/new.py'),))),
        ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Reviewer', query='review')]),
        ScriptedTurn(content=review),
        ScriptedTurn(content=answer),
    ]


class _Meddling(ScriptedLLM):
    """第 `at` 轮之前动一下宿主环境：模拟 run 期间有东西改了仓库配置、钩子或提交历史。"""

    def __init__(self, script: list[ScriptedTurn], *, at: int, meddle: Callable[[], object]):
        super().__init__(script)
        self._at = at
        self._meddle = meddle
        self._count = 0

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        if self._count == self._at:
            self._meddle()
        self._count += 1
        return await super().create(messages, **kwargs)


class _Answering(StringIO):
    """读答复之前先做一件事：模拟人在问题挂着的时候改了文件。"""

    def __init__(self, answer: str, before: Callable[[], None]) -> None:
        super().__init__(answer)
        self._before = before

    def readline(self, size: int | None = -1) -> str:
        self._before()
        return super().readline(-1 if size is None else size)


class _Run:
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def matter(self) -> dict[str, str]:
        head = self.stdout.split('---\n')[1]
        return dict(line.split(': ', 1) for line in head.splitlines())

    @property
    def git(self) -> str:
        return self.matter['git']


def _run(
    tmp_path: Path,
    llm: ScriptedLLM | list[ScriptedTurn],
    *,
    root: Path,
    git_mode: str | None = None,
    stdin: str | TextIO = '',
    verify: Sequence[str] | None = ('true',),
    protected: list[str] | None = None,
    flags: Sequence[str] = (),
    stderr: StringIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> _Run:
    model = llm if isinstance(llm, ScriptedLLM) else ScriptedLLM(llm)
    config = _config(tmp_path, verify, protected)
    stdout = StringIO()
    stderr = StringIO() if stderr is None else stderr
    io = CliIO(
        stdin=StringIO(stdin) if isinstance(stdin, str) else stdin, stdout=stdout, stderr=stderr
    )
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: model,
        workspace_root=root,
        environ=_env(tmp_path) if environ is None else dict(environ),
    )
    extra = [*flags, *(['--git-mode', git_mode] if git_mode is not None else [])]
    try:
        code = main(
            ['--config', str(config), 'run', '--handoff', *extra, '--agent', 'Lead', 'work on it'],
            host=host,
            io=io,
        )
    finally:
        host.close()
    return _Run(code, stdout.getvalue(), stderr.getvalue())


def _head(root: Path, tmp_path: Path) -> str:
    return _git(root, tmp_path, 'rev-parse', 'HEAD').strip()


def _committed_paths(root: Path, tmp_path: Path, rev: str = 'HEAD') -> set[str]:
    raw = _git(root, tmp_path, 'diff-tree', '-r', '-z', '--name-only', '--no-commit-id', rev)
    return {name for name in raw.split('\0') if name}


def _remote_head(tmp_path: Path) -> str:
    return _git(tmp_path / 'remote.git', tmp_path, 'rev-parse', 'main').strip()


def _message(root: Path, tmp_path: Path) -> str:
    return _git(root, tmp_path, 'cat-file', 'commit', 'HEAD').split('\n\n', 1)[1]


def _status(root: Path, tmp_path: Path) -> list[str]:
    raw = _git(root, tmp_path, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    return sorted(entry for entry in raw.split('\0') if entry)


# ---- 纯函数 ----


def _integrity(**overrides: object) -> Integrity:
    values: dict[str, Any] = {
        'toplevel': '/repo',
        'head': 'a' * 40,
        'branch': 'main',
        'upstream': ('origin', 'refs/heads/main'),
        'upstream_head': 'a' * 40,
        'push_urls': ('git@example.com:o/r.git',),
        'config': 'c' * 64,
        'hooks': (),
        'in_progress': (),
    }
    values.update(overrides)
    return Integrity(**values)


def _offer(**overrides: Any) -> Offer:
    arguments: dict[str, Any] = {
        'root': Path('/repo'),
        'before': _integrity(),
        'now': _integrity(),
        'verification': (),
        'run_changes': [Change('src/new.py', '??', 'b' * 40, mode='100644')],
        'touched_again': (),
        'before_changes': (),
        'ignored_before': frozenset(),
        'protected': (),
        'ahead': 0,
    }
    arguments.update(overrides)
    return offer(**arguments)


def test_all_preconditions_met_offers_everything():
    choices = _offer()

    assert (choices.commit, choices.push, choices.reasons, choices.push_reason) == (
        True,
        True,
        (),
        None,
    )


@pytest.mark.parametrize(
    ('overrides', 'reason'),
    [
        ({'before': None}, '取不到仓库的完整性快照'),
        ({'now': None}, '取不到仓库的完整性快照'),
        ({'now': _integrity(config='d' * 64)}, '完整性快照与 run 开始前不一致'),
        ({'now': _integrity(hooks=(('pre-commit', 'e' * 40),))}, '钩子 pre-commit 变了'),
        ({'now': _integrity(upstream_head='f' * 40)}, '上游分支在本地的位置'),
        ({'now': _integrity(push_urls=('/elsewhere.git',))}, '推送地址'),
        ({'before_changes': None}, '运行前的快照没取到'),
        ({'ignored_before': None}, '列不出运行前被忽略的文件'),
        ({'ignored_before': frozenset({'build/'})}, None),
        ({'verification': ['宿主复核没有全过']}, '宿主复核没有全过'),
        ({'now': _integrity(in_progress=('merge',))}, '有进行中的 merge'),
        ({'run_changes': []}, '这次 run 没有改动'),
        ({'touched_again': ('src/new.py',)}, '有运行前就改过、这次又被改的文件'),
        ({'ignored_before': frozenset({'src/'})}, '运行前被忽略的文件出现在改动里：src/new.py'),
        ({'root': Path('/repo/src')}, '工作区根不是 git 顶层目录'),
    ],
)
def test_each_unmet_commit_precondition_is_named(overrides: dict[str, Any], reason: str | None):
    choices = _offer(**overrides)

    if reason is None:
        assert choices.commit
    else:
        assert not choices.commit and not choices.push
        assert any(reason in item for item in choices.reasons), choices.reasons


def test_every_unmet_precondition_is_listed_not_just_the_first():
    choices = _offer(verification=['宿主复核没有全过'], run_changes=[], before_changes=None)

    assert len(choices.reasons) == 3


def test_every_changed_part_of_the_snapshot_is_named():
    now = _integrity(config='d' * 64, hooks=(('pre-commit', 'e' * 40),), head='b' * 40)

    (reason,) = _offer(now=now).reasons

    assert reason == (
        '仓库的完整性快照与 run 开始前不一致（HEAD、git 配置（`git config --list` 的结果）、'
        '钩子 pre-commit 变了）'
    )


def test_a_tampered_repository_gets_inspect_first_advice_not_commit_advice():
    assert _offer(now=_integrity(config='d' * 64)).tampered
    assert not _offer(verification=['宿主复核没有全过']).tampered


@pytest.mark.parametrize(
    ('status', 'host_verify', 'verdict', 'gaps'),
    [
        ('verified', 'passed', 'pass', []),
        ('run_failed', 'skipped', 'unverified', ['run 没有正常结束']),
        ('verification_failed', 'failed', 'pass', ['宿主复核没有全过']),
        (
            'unverified',
            'not_configured',
            'block',
            ['没有配置 workspace.verify：宿主没有复核', 'Reviewer 结论是 block，不是 pass'],
        ),
        ('unverified', 'passed', 'partial', ['Reviewer 结论是 partial，不是 pass']),
    ],
)
def test_each_reason_for_not_being_verified_is_named(
    status: str, host_verify: str, verdict: str, gaps: list[str]
):
    assert verification_gaps(status=status, host_verify=host_verify, verdict=verdict) == gaps


@pytest.mark.parametrize(
    ('overrides', 'reason'),
    [
        ({'before': _integrity(upstream=None), 'now': _integrity(upstream=None)}, '没有上游'),
        ({'ahead': None}, '算不出本地领先上游几个提交'),
        ({'ahead': 2}, '本地已经领先上游 2 个提交'),
        ({'ahead': 1}, '本地已经领先上游 1 个提交'),
        (
            {
                'before': _integrity(hooks=(('pre-push', 'x ' + 'e' * 40),)),
                'now': _integrity(hooks=(('pre-push', 'x ' + 'e' * 40),)),
            },
            '推送时会在本地运行的钩子（pre-push）',
        ),
        (
            {
                'before': _integrity(hooks=(('reference-transaction', 'x ' + 'e' * 40),)),
                'now': _integrity(hooks=(('reference-transaction', 'x ' + 'e' * 40),)),
            },
            '（reference-transaction）',
        ),
        (
            {'before_changes': [Change('notes.txt', '??', 'b' * 40)]},
            '运行前工作区里就有 1 处改动',
        ),
    ],
)
def test_unmet_push_preconditions_still_offer_a_commit(overrides: dict[str, Any], reason: str):
    choices = _offer(**overrides)

    assert choices.commit and not choices.push
    assert choices.push_reason is not None and reason in choices.push_reason


def test_protected_changes_do_not_block_the_offer_itself():
    choices = _offer(protected=['pyproject.toml', '.gitignore'])

    assert choices.commit and choices.push
    assert choices.protected == ('.gitignore', 'pyproject.toml')


def test_the_summary_caps_the_list_at_30_and_escapes_paths():
    changes = [Change(f'f{index:02}\x1b.txt', '??', 'b' * 40) for index in range(31)]

    text = summary(
        before=_integrity(),
        changes=changes,
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
    )

    assert text.count('\n- `??` ') == 30
    assert '- ……共 31 处' in text
    assert '\x1b' not in text and 'f00\\x1b.txt' in text
    assert 'f30' not in text


def test_taking_the_integrity_snapshot_writes_nothing_into_the_object_store(tmp_path: Path):
    root = _repository(tmp_path)
    _hook(tmp_path, 'pre-commit', 'exit 0 # never stored')

    def objects() -> list[Path]:
        return sorted(p for p in (root / '.git' / 'objects').rglob('*') if p.is_file())

    stored = objects()
    snapshot = integrity(HostGit(root, _env(tmp_path)))

    assert snapshot.hooks and objects() == stored


@pytest.mark.parametrize(
    ('marker', 'label', 'directory'),
    [
        ('MERGE_HEAD', 'merge', False),
        ('rebase-merge', 'rebase', True),
        ('rebase-apply', 'rebase 或 am', True),
        ('CHERRY_PICK_HEAD', 'cherry-pick', False),
        ('REVERT_HEAD', 'revert', False),
        ('BISECT_LOG', 'bisect', False),
    ],
)
def test_each_operation_in_progress_is_recognised(
    tmp_path: Path, marker: str, label: str, directory: bool
):
    root = _repository(tmp_path)
    path = root / '.git' / marker
    if directory:
        path.mkdir()
    else:
        path.write_text(_head(root, tmp_path) + '\n', encoding='utf-8')

    assert integrity(HostGit(root, _env(tmp_path))).in_progress == (label,)


def test_the_summary_lists_exactly_30_changes_without_a_total():
    changes = [Change(f'f{index:02}.txt', '??', 'b' * 40) for index in range(30)]

    text = summary(
        before=_integrity(),
        changes=changes,
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
    )

    assert text.count('\n- `??` ') == 30
    assert '……共' not in text


def test_the_summary_names_pre_existing_changes_and_the_hooks_that_will_run():
    text = summary(
        before=_integrity(
            hooks=(('commit-msg', 'x ' + 'e' * 40), ('pre-commit.sample', 'x ' + 'f' * 40))
        ),
        changes=[Change('src/new.py', '??', 'b' * 40)],
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=2,
        title='修好 \x1b calc',
    )

    assert '提交标题：修好 \\x1b calc' in text.splitlines()
    assert '运行前就有的 2 处改动不进提交' in text
    assert '提交时会运行钩子（在 Agent 的环境里，拿不到 ssh agent 与密钥）：commit-msg' in text
    assert 'pre-commit.sample' not in text


def test_the_summary_names_only_executable_hooks_that_a_commit_runs():
    hooks = (
        ('commit-msg', 'x ' + 'a' * 40),
        ('post-commit', '- ' + 'b' * 40),
        ('post-index-change', 'x ' + 'c' * 40),
        ('pre-push', 'x ' + 'd' * 40),
        ('reference-transaction', 'x ' + 'e' * 40),
    )
    text = summary(
        before=_integrity(hooks=hooks),
        changes=[Change('src/new.py', '??', 'b' * 40)],
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
    )

    line = '提交时会运行钩子（在 Agent 的环境里，拿不到 ssh agent 与密钥）：'
    assert f'{line}commit-msg、post-index-change、reference-transaction' in text.splitlines()


def test_the_summary_names_every_hook_a_commit_can_run():
    names = (
        'commit-msg',
        'post-commit',
        'post-index-change',
        'pre-auto-gc',
        'pre-commit',
        'prepare-commit-msg',
        'reference-transaction',
    )
    text = summary(
        before=_integrity(hooks=tuple((name, 'x ' + 'a' * 40) for name in names)),
        changes=[Change('src/new.py', '??', 'b' * 40)],
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
    )

    line = '提交时会运行钩子（在 Agent 的环境里，拿不到 ssh agent 与密钥）：'
    assert f'{line}{"、".join(names)}' in text.splitlines()


def test_a_commit_hook_named_in_another_case_is_still_named():
    text = summary(
        before=_integrity(hooks=(('Pre-Commit', 'x ' + 'a' * 40),)),
        changes=[Change('src/new.py', '??', 'b' * 40)],
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
    )

    line = '提交时会运行钩子（在 Agent 的环境里，拿不到 ssh agent 与密钥）：'
    assert f'{line}Pre-Commit' in text.splitlines()


def test_the_summary_has_no_hook_line_when_only_push_time_hooks_exist():
    text = summary(
        before=_integrity(hooks=(('pre-push', 'x ' + 'd' * 40),)),
        changes=[Change('src/new.py', '??', 'b' * 40)],
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
    )

    assert '提交时会运行钩子' not in text


# ---- 增删行数 ----


def _counts(root: Path, tmp_path: Path) -> dict[str, str]:
    git = HostGit(root, _env(tmp_path))
    return line_counts(git, git.changes())


def test_the_summary_puts_each_count_in_a_column_before_its_path_in_the_order_given():
    changes = [
        Change('c.py', ' M', 'b' * 40),
        Change('a.py', '??', 'b' * 40),
        Change('b.bin', '??', 'b' * 40),
        Change('d.txt', '??', 'b' * 40),
        Change('e\x1b.txt', '??', 'b' * 40),
    ]

    text = summary(
        before=_integrity(),
        changes=changes,
        written=lambda path: path != 'a.py',
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
        counts={'b.bin': '二进制', 'a.py': '+3 −0', 'c.py': '+2 −1', 'e\x1b.txt': '+1 −0'},
    )

    assert [line for line in text.splitlines() if line.startswith('- `')] == [
        '- ` M` `+2 −1` c.py',
        '- `??` `+3 −0` a.py（不是 Write / Edit 产生的）',
        '- `??` `二进制` b.bin',
        '- `??` `?` d.txt',
        '- `??` `+1 −0` e\\x1b.txt',
    ]


def test_a_file_name_that_looks_like_a_count_cannot_take_the_count_column():
    text = summary(
        before=_integrity(),
        changes=[Change('notes.md `+1 −0`', '??', 'b' * 40)],
        written=lambda path: True,
        host_verify='passed',
        verdict='pass',
        protected=(),
        pre_existing=0,
        title='t',
        counts={'notes.md `+1 −0`': '+4000 −0'},
    )

    assert '- `??` `+4000 −0` notes.md `+1 −0`' in text


def test_tracked_files_are_counted_by_git_and_new_files_by_their_lines(tmp_path: Path):
    root = _with_tracked(
        tmp_path, {'z.txt': 'a\nb\nc\n', 'm.txt': 'one\ntwo\n', 'gone.txt': 'x\ny\n'}
    )
    (root / 'z.txt').write_text('a\nB\nc\nd\n', encoding='utf-8')
    (root / 'gone.txt').unlink()
    (root / 'm.txt').write_text('one\ntwo\nthree', encoding='utf-8')
    (root / 'b_new.txt').write_text('1\n2\n3', encoding='utf-8')

    assert _counts(root, tmp_path) == {
        'b_new.txt': '+3 −0',
        'gone.txt': '+0 −2',
        'm.txt': '+1 −0',
        'z.txt': '+2 −1',
    }


def test_a_binary_file_is_counted_as_binary_whether_new_or_tracked(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'old.bin').write_bytes(b'\0\1\2')
    _git(root, tmp_path, 'add', 'old.bin')
    _git(root, tmp_path, 'commit', '-q', '-m', 'binary')
    (root / 'old.bin').write_bytes(b'\0\1\3')
    (root / 'new.bin').write_bytes(b'header\0' + b'x' * 100)

    assert _counts(root, tmp_path) == {'new.bin': '二进制', 'old.bin': '二进制'}


def test_a_new_file_is_binary_only_by_its_first_8000_bytes_as_git_decides(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'late.txt').write_bytes(b'a\n' * 5000 + b'\0')

    assert _counts(root, tmp_path) == {'late.txt': '+5001 −0'}


def test_a_new_file_over_the_size_limit_is_not_read_before_the_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    monkeypatch.setattr(git_finish, '_COUNT_LIMIT', 2 * 1024 * 1024)
    (root / 'at_limit.txt').write_bytes(b'x\n' * (1024 * 1024))
    with (root / 'over_limit.bin').open('wb') as stream:
        stream.truncate(3 * 1024 * 1024 + 1)

    assert _counts(root, tmp_path) == {
        'at_limit.txt': '+1048576 −0',
        'over_limit.bin': '大文件，3 MiB，没有数行',
    }


def test_a_new_symlink_counts_its_link_text_and_is_not_followed(tmp_path: Path):
    root = _repository(tmp_path)
    outside = tmp_path / 'outside.txt'
    outside.write_text('line\n' * 50, encoding='utf-8')
    (root / 'link').symlink_to(outside)

    assert _counts(root, tmp_path) == {'link': '+1 −0'}


def test_a_staged_rename_is_counted_against_the_old_content(tmp_path: Path):
    root = _with_tracked(tmp_path, {'old.txt': 'a\nb\nc\nd\ne\n'})
    _git(root, tmp_path, 'config', 'diff.renames', 'false')
    _git(root, tmp_path, 'config', 'status.renames', 'true')
    _git(root, tmp_path, 'mv', 'old.txt', 'new.txt')
    (root / 'new.txt').write_text('a\nb\nc\nd\ne\nf\n', encoding='utf-8')
    git = HostGit(root, _env(tmp_path))
    changes = git.changes()

    assert [(change.path, change.origin) for change in changes] == [('new.txt', 'old.txt')]
    assert line_counts(git, changes) == {'new.txt': '+1 −0'}


def test_counts_in_a_workspace_below_the_repository_top_use_workspace_paths(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'src' / 'keep.py').write_text('KEEP = 2\n', encoding='utf-8')
    (root / 'src' / 'add.py').write_text('A = 1\n', encoding='utf-8')
    git = HostGit(root / 'src', _env(tmp_path))

    assert line_counts(git, git.changes()) == {'add.py': '+1 −0', 'keep.py': '+1 −1'}


def test_a_special_file_has_no_count_and_never_blocks(tmp_path: Path):
    root = _repository(tmp_path)
    os.mkfifo(root / 'pipe')

    counts = line_counts(HostGit(root, _env(tmp_path)), [Change('pipe', '??', None)])

    assert counts == {}


def test_a_regular_file_swapped_for_a_fifo_after_lstat_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    os.mkfifo(root / 'pipe')
    (root / 'plain.txt').write_text('x\n', encoding='utf-8')
    real_lstat = os.lstat

    def regular(path: Any) -> os.stat_result:
        if Path(path).name == 'pipe':
            return real_lstat(root / 'plain.txt')
        return real_lstat(path)

    monkeypatch.setattr(git_finish.os, 'lstat', regular)

    counts = line_counts(HostGit(root, _env(tmp_path)), [Change('pipe', '??', None)])

    assert counts == {}


def test_a_failing_git_diff_leaves_tracked_changes_without_a_count(tmp_path: Path):
    root = tmp_path / 'fresh'
    _git(tmp_path, tmp_path, 'init', '-q', '-b', 'main', str(root))
    (root / 'staged.txt').write_text('x\n', encoding='utf-8')
    (root / 'new.txt').write_text('y\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'staged.txt')
    changes = [Change('new.txt', '??', 'b' * 40), Change('staged.txt', 'A ', 'b' * 40)]

    assert line_counts(HostGit(root, _env(tmp_path)), changes) == {'new.txt': '+1 −0'}


def test_an_interrupted_hooked_commit_stops_its_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    marker = tmp_path / 'hook.pid'
    _hook(tmp_path, 'pre-commit', f'echo $$ > {marker}\nexec sleep 30')
    (root / 'a.txt').write_text('x\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'a.txt')
    original = subprocess.Popen.communicate
    interrupted: list[bool] = []

    def communicate(self: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> Any:
        if interrupted:
            return original(self, *args, **kwargs)
        interrupted.append(True)
        deadline = time.monotonic() + _HOOK_STARTS_WITHIN
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, 'communicate', communicate)

    with pytest.raises(KeyboardInterrupt):
        git_finish._hooked(  # pyright: ignore[reportPrivateUsage]
            HostGit(root, _env(tmp_path)),
            _env(tmp_path),
            RuleRedactor(),
            ['commit', '-q', '-F', '-'],
            message='m',
        )

    pid = int(marker.read_text(encoding='utf-8'))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f'the hook process {pid} outlived the interrupted commit')


def test_a_nul_inside_the_first_8000_bytes_makes_a_new_file_binary_and_one_after_does_not(
    tmp_path: Path,
):
    root = _repository(tmp_path)
    (root / 'early.bin').write_bytes(b'a' * 7999 + b'\0')
    (root / 'late.txt').write_bytes(b'a' * 8000 + b'\0')

    assert _counts(root, tmp_path) == {'early.bin': '二进制', 'late.txt': '+1 −0'}


def test_a_new_file_bigger_than_one_read_is_counted_to_the_end(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'many.txt').write_bytes(b'x\n' * 100_000)

    assert _counts(root, tmp_path) == {'many.txt': '+100000 −0'}


def test_a_new_file_is_read_only_up_to_its_size_when_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    (root / 'growing.log').write_bytes(b'a\nb\nc\n')
    real_fstat = os.fstat

    def smaller(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        fields = list(info[:10])
        fields[6] = 2
        return os.stat_result(fields)

    monkeypatch.setattr(git_finish.os, 'fstat', smaller)

    assert line_counts(HostGit(root, _env(tmp_path)), [Change('growing.log', '??', None)]) == {
        'growing.log': '+1 −0'
    }


def test_a_binary_new_file_is_read_no_further_than_it_takes_to_tell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    (root / 'image.bin').write_bytes(b'\0' + b'x' * (4 * 1024 * 1024))
    reads: list[int] = []
    real_read = os.read

    def counted(descriptor: int, size: int) -> bytes:
        reads.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(git_finish.os, 'read', counted)

    counts = line_counts(HostGit(root, _env(tmp_path)), [Change('image.bin', '??', None)])

    assert counts == {'image.bin': '二进制'}
    assert len(reads) == 1


def test_a_symlink_swapped_in_after_lstat_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    target = tmp_path / 'outside.txt'
    target.write_text('line\n' * 50, encoding='utf-8')
    (root / 'swapped').symlink_to(target)
    (root / 'plain.txt').write_text('x\n', encoding='utf-8')
    real_lstat = os.lstat

    def regular(path: Any) -> os.stat_result:
        if Path(path).name == 'swapped':
            return real_lstat(root / 'plain.txt')
        return real_lstat(path)

    monkeypatch.setattr(git_finish.os, 'lstat', regular)

    counts = line_counts(HostGit(root, _env(tmp_path)), [Change('swapped', '??', None)])

    assert counts == {}


def test_a_symlink_that_goes_away_before_it_is_read_has_no_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    (root / 'link').symlink_to('target')

    def gone(path: Any) -> str:
        raise FileNotFoundError(path)

    monkeypatch.setattr(git_finish.os, 'readlink', gone)

    assert line_counts(HostGit(root, _env(tmp_path)), [Change('link', '??', None)]) == {}


def test_a_tracked_file_with_a_tab_in_its_name_is_counted(tmp_path: Path):
    root = _with_tracked(tmp_path, {'a\tb.txt': 'one\n'})
    (root / 'a\tb.txt').write_text('two\n', encoding='utf-8')

    assert _counts(root, tmp_path) == {'a\tb.txt': '+1 −1'}


def test_a_rename_git_cannot_pair_is_counted_as_the_new_lines_and_the_old_ones(tmp_path: Path):
    root = _with_tracked(tmp_path, {'old.txt': ''.join(f'line {i}\n' for i in range(20))})
    _git(root, tmp_path, 'config', 'status.renames', 'true')
    _git(root, tmp_path, 'mv', 'old.txt', 'new.txt')
    (root / 'new.txt').write_text(''.join(f'other {i}\n' for i in range(5)), encoding='utf-8')
    git = HostGit(root, _env(tmp_path))
    changes = git.changes()

    assert [(change.path, change.origin) for change in changes] == [('new.txt', 'old.txt')]
    assert line_counts(git, changes) == {'new.txt': '+5 −20'}


_TEXT = b''.join(b'line %d\n' % number for number in range(20))
_BINARY = b'\0' + b'z' * 200


@pytest.mark.parametrize(
    ('old', 'old_content', 'new', 'new_content'),
    [('old.txt', _TEXT, 'new.bin', _BINARY), ('old.bin', _BINARY, 'new.txt', _TEXT)],
    ids=['text_became_binary', 'binary_became_text'],
)
def test_a_rename_git_cannot_pair_with_a_binary_half_is_counted_as_binary(
    tmp_path: Path, old: str, old_content: bytes, new: str, new_content: bytes
):
    root = _repository(tmp_path)
    (root / old).write_bytes(old_content)
    _git(root, tmp_path, 'add', old)
    _git(root, tmp_path, 'commit', '-q', '-m', 'old')
    _git(root, tmp_path, 'config', 'status.renames', 'true')
    _git(root, tmp_path, 'mv', old, new)
    (root / new).write_bytes(new_content)
    git = HostGit(root, _env(tmp_path))
    changes = git.changes()

    assert [(change.path, change.origin) for change in changes] == [(new, old)]
    assert line_counts(git, changes) == {new: '二进制'}


def test_an_empty_new_file_adds_no_lines(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'empty.txt').write_bytes(b'')

    assert _counts(root, tmp_path) == {'empty.txt': '+0 −0'}


def _hooked_commit(tmp_path: Path, root: Path) -> Any:
    return git_finish._hooked(  # pyright: ignore[reportPrivateUsage]
        HostGit(root, _env(tmp_path)),
        _env(tmp_path),
        RuleRedactor(),
        ['commit', '-q', '-F', '-'],
        message='m',
    )


def _interrupted_communicate(monkeypatch: pytest.MonkeyPatch, marker: Path, times: int) -> None:
    """前 `times` 次 `communicate` 等钩子写下 pid 之后抛 KeyboardInterrupt（人按 Ctrl-C），
    之后照常。"""
    original = subprocess.Popen.communicate
    calls: list[bool] = []

    def communicate(self: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> Any:
        if len(calls) >= times:
            return original(self, *args, **kwargs)
        calls.append(True)
        deadline = time.monotonic() + _HOOK_STARTS_WITHIN
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, 'communicate', communicate)


def _assert_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    pytest.fail(f'process {pid} outlived the interrupted commit')


def test_a_second_ctrl_c_while_the_hook_is_stopped_still_kills_a_hook_ignoring_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    marker = tmp_path / 'hook.pid'
    _hook(tmp_path, 'pre-commit', f"trap '' TERM\necho $$ > {marker}\nwhile :; do sleep 1; done")
    (root / 'a.txt').write_text('x\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'a.txt')
    _interrupted_communicate(monkeypatch, marker, times=2)

    with pytest.raises(KeyboardInterrupt):
        _hooked_commit(tmp_path, root)

    _assert_process_gone(int(marker.read_text(encoding='utf-8')))


def test_a_group_left_with_only_zombies_does_not_turn_ctrl_c_into_another_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    marker = tmp_path / 'hook.pid'
    _hook(tmp_path, 'pre-commit', f'echo $$ > {marker}\nexec sleep 30')
    (root / 'a.txt').write_text('x\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'a.txt')
    _interrupted_communicate(monkeypatch, marker, times=1)
    real_killpg = os.killpg

    def eperm_then_kill(pgid: int, signum: int) -> None:
        real_killpg(pgid, signal.SIGKILL)
        raise PermissionError(1, 'Operation not permitted')

    monkeypatch.setattr(git_finish.os, 'killpg', eperm_then_kill)

    with pytest.raises(KeyboardInterrupt):
        _hooked_commit(tmp_path, root)


def test_ctrl_c_during_the_commit_unstages_the_new_files_it_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    (root / 'src' / 'new.py').write_text('NEW = 1\n', encoding='utf-8')
    (root / 'README.md').write_text('# changed\n', encoding='utf-8')
    git = HostGit(root, _env(tmp_path))
    before = integrity(git)
    changes = git.changes()
    real_hooked = git_finish._hooked  # pyright: ignore[reportPrivateUsage]

    def interrupted_commit(*args: Any, **kwargs: Any) -> Any:
        if args[3][0] == 'commit':
            raise KeyboardInterrupt
        return real_hooked(*args, **kwargs)

    monkeypatch.setattr(git_finish, '_hooked', interrupted_commit)

    with pytest.raises(git_finish.GitInterrupted) as excinfo:
        git_finish.execute(
            git,
            changes=changes,
            message='m\n',
            push=False,
            before=before,
            redactor=RuleRedactor(),
            hook_env=_env(tmp_path),
        )

    status = _status(root, tmp_path)
    assert '?? src/new.py' in status and ' M README.md' in status
    assert excinfo.value.detail == '没有提交；改动还在工作区里；为提交暂存的新文件已经撤回。'


def test_ctrl_c_when_head_cannot_be_read_leaves_the_index_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    (root / 'src' / 'new.py').write_text('NEW = 1\n', encoding='utf-8')
    git = HostGit(root, _env(tmp_path))
    before = integrity(git)
    changes = git.changes()
    real_hooked = git_finish._hooked  # pyright: ignore[reportPrivateUsage]
    real_attempt = git_finish._attempt  # pyright: ignore[reportPrivateUsage]
    broken: list[bool] = []

    def interrupted_commit(*args: Any, **kwargs: Any) -> Any:
        if args[3][0] == 'commit':
            broken.append(True)
            raise KeyboardInterrupt
        return real_hooked(*args, **kwargs)

    def head_unreadable(git: HostGit, *args: str) -> str | None:
        if broken and args[:1] == ('rev-parse',):
            return None
        return real_attempt(git, *args)

    monkeypatch.setattr(git_finish, '_hooked', interrupted_commit)
    monkeypatch.setattr(git_finish, '_attempt', head_unreadable)

    with pytest.raises(git_finish.GitInterrupted) as excinfo:
        git_finish.execute(
            git,
            changes=changes,
            message='m\n',
            push=False,
            before=before,
            redactor=RuleRedactor(),
            hook_env=_env(tmp_path),
        )

    assert excinfo.value.detail.startswith('确认不了有没有提交')
    assert 'A  src/new.py' in _status(root, tmp_path)


@pytest.mark.parametrize(
    ('push', 'said'),
    [(True, '推送做到了哪一步不知道'), (False, '没有推送：先看这个提交')],
    ids=['commit_and_push', 'commit_only'],
)
def test_ctrl_c_after_the_commit_was_made_leaves_the_index_alone_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, push: bool, said: str
):
    root = _repository(tmp_path)
    (root / 'src' / 'new.py').write_text('NEW = 1\n', encoding='utf-8')
    git = HostGit(root, _env(tmp_path))
    before = integrity(git)
    changes = git.changes()
    real_hooked = git_finish._hooked  # pyright: ignore[reportPrivateUsage]

    def committed_then_interrupted(*args: Any, **kwargs: Any) -> Any:
        completed = real_hooked(*args, **kwargs)
        if args[3][0] == 'commit':
            raise KeyboardInterrupt
        return completed

    monkeypatch.setattr(git_finish, '_hooked', committed_then_interrupted)

    with pytest.raises(git_finish.GitInterrupted) as excinfo:
        git_finish.execute(
            git,
            changes=changes,
            message='m\n',
            push=push,
            before=before,
            redactor=RuleRedactor(),
            hook_env=_env(tmp_path),
        )

    head = _head(root, tmp_path)
    assert head != before.head
    assert excinfo.value.detail.startswith(f'提交已经在本地（{head}），{said}')
    assert 'src/new.py' not in _status(root, tmp_path)
    assert _remote_head(tmp_path) == before.head


def test_ctrl_c_that_leaves_an_index_lock_behind_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _repository(tmp_path)
    (root / 'src' / 'new.py').write_text('NEW = 1\n', encoding='utf-8')
    git = HostGit(root, _env(tmp_path))
    before = integrity(git)
    changes = git.changes()
    real_hooked = git_finish._hooked  # pyright: ignore[reportPrivateUsage]

    def killed_mid_commit(*args: Any, **kwargs: Any) -> Any:
        if args[3][0] == 'commit':
            (root / '.git' / 'index.lock').write_bytes(b'')
            raise KeyboardInterrupt
        return real_hooked(*args, **kwargs)

    monkeypatch.setattr(git_finish, '_hooked', killed_mid_commit)

    with pytest.raises(git_finish.GitInterrupted) as excinfo:
        git_finish.execute(
            git,
            changes=changes,
            message='m\n',
            push=False,
            before=before,
            redactor=RuleRedactor(),
            hook_env=_env(tmp_path),
        )

    detail = excinfo.value.detail
    assert '这些新文件还在暂存区里：src/new.py' in detail
    assert '仓库里留下了 ' in detail
    assert '/.git/index.lock：确认没有别的 git 进程在跑之后删掉它' in detail


def test_stopping_returns_even_if_a_process_that_left_the_group_holds_its_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(git_finish, '_STOP_GRACE', 0.3)
    pid_file = tmp_path / 'escaped.pid'
    escaped = (
        'import os, time\n'
        f'open({str(pid_file)!r} + ".tmp", "w").write(str(os.getpid()))\n'
        f'os.replace({str(pid_file)!r} + ".tmp", {str(pid_file)!r})\n'
        'time.sleep(20)\n'
    )
    leader = (
        'import subprocess, sys, time\n'
        f'subprocess.Popen([sys.executable, "-c", {escaped!r}], start_new_session=True)\n'
        'time.sleep(20)\n'
    )
    process = subprocess.Popen(
        [sys.executable, '-c', leader],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + _HOOK_STARTS_WITHIN
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    escaped_pid = int(pid_file.read_text(encoding='utf-8'))
    try:
        started = time.monotonic()
        git_finish._stop(process)  # pyright: ignore[reportPrivateUsage]
        elapsed = time.monotonic() - started
    finally:
        os.kill(escaped_pid, signal.SIGKILL)
    assert elapsed < 5


def test_an_answer_that_is_not_utf8_means_3():
    stdin = TextIOWrapper(BytesIO(b'\xff\xfe1\n'), encoding='utf-8')

    assert ask(stdin, StringIO(), 'summary', Offer((), None, ())) == '3'


def test_verify_commands_in_the_commit_body_are_escaped():
    message = commit_message('t', 'goal', [('true\t#x', 'passed')])

    assert message == 't\n\n宿主复核：\n- 通过：true\\t#x —— passed\n'


def test_the_commit_title_is_escaped_and_cut_to_72_characters():
    long = 'x' * 80

    assert commit_message('y' * 72, 'goal', []).split('\n', 1)[0] == 'y' * 72
    assert commit_message(long, 'goal', []).split('\n', 1)[0] == 'x' * 71 + '…'
    assert commit_message('a\x1b[31mb', 'goal', []).startswith('a\\x1b[31mb\n')
    assert commit_message(None, '\n  \nfirst line\nsecond', []).startswith('first line\n')


# ---- 选项与答复 ----


def test_answering_1_commits_this_runs_change_and_pushes_it(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert (run.code, run.git) == (0, 'pushed')
    assert _committed_paths(root, tmp_path) == {'src/new.py'}
    assert _remote_head(tmp_path) == _head(root, tmp_path)
    assert f'提交：`{_head(root, tmp_path)}`\n已推送。' in run.stdout
    assert '选择：提交并推送。' in run.stdout


def test_answering_2_commits_without_pushing(tmp_path: Path):
    root = _repository(tmp_path)
    pushed_before = _remote_head(tmp_path)

    run = _run(tmp_path, _script(), root=root, stdin='2\n')

    assert (run.code, run.git) == (0, 'committed')
    assert _committed_paths(root, tmp_path) == {'src/new.py'}
    assert _remote_head(tmp_path) == pushed_before != _head(root, tmp_path)


@pytest.mark.parametrize('answer', ['3\n', '\n', '', 'yes\n', '12\n', 'push\n'])
def test_3_an_empty_line_eof_and_anything_else_leave_the_change_uncommitted(
    tmp_path: Path, answer: str
):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(), root=root, stdin=answer)

    assert (run.code, run.git) == (0, 'declined')
    assert _head(root, tmp_path) == head
    assert '?? src/new.py' in _status(root, tmp_path)
    assert '人选了「我来处理」' in run.stdout


def test_the_question_is_visible_before_the_answer_is_read(tmp_path: Path):
    root = _repository(tmp_path)
    stderr = StringIO()
    seen: list[str] = []

    run = _run(
        tmp_path,
        _script(),
        root=root,
        stdin=_Answering('3\n', lambda: seen.append(stderr.getvalue())),
        stderr=stderr,
    )

    assert run.git == 'declined'
    assert seen and seen[0].rstrip().endswith('选哪个 [1/2/3]？>')


def test_a_read_only_run_does_not_ask_or_offer_anything(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, [ScriptedTurn(content='done')], root=root, flags=['--no-write'])

    assert (run.code, run.matter['status'], run.git) == (0, 'read_only', 'not_offered')
    assert '只读 run 不改动工作区，不做 git 收尾。' in run.stdout
    assert '选哪个' not in run.stderr


def test_the_question_goes_to_stderr_with_all_three_options(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root)

    assert '1. 提交并推送' in run.stderr
    assert '2. 只提交' in run.stderr
    assert '3. 我来处理' in run.stderr
    assert run.stderr.rstrip().endswith('选哪个 [1/2/3]？>')
    assert '选哪个' not in run.stdout
    upstream = f'git 收尾：分支 main → 上游 origin/main（推送到 {tmp_path / "remote.git"}）'
    assert upstream in run.stderr
    assert '宿主复核：passed；Reviewer 结论（模型给出）：pass' in run.stderr
    assert '提交标题：add new' in run.stderr.splitlines()


def test_without_an_upstream_only_2_and_3_are_offered_and_1_is_refused(tmp_path: Path):
    root = _repository(tmp_path, upstream=False)
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert '1. 提交并推送' not in run.stderr
    assert '（没有推送选项：当前分支没有上游）' in run.stderr
    assert '选哪个 [2/3]？>' in run.stderr
    assert (run.code, run.git) == (0, 'declined')
    assert _head(root, tmp_path) == head


def test_with_unpushed_local_commits_push_is_not_offered(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'local.txt').write_text('local\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'local.txt')
    _git(root, tmp_path, 'commit', '-q', '-m', 'local only')
    pushed_before = _remote_head(tmp_path)

    run = _run(tmp_path, _script(), root=root, stdin='2\n')

    assert '1. 提交并推送' not in run.stderr
    assert '本地已经领先上游 1 个提交' in run.stderr
    assert (run.code, run.git) == (0, 'committed')
    assert '只提交，没有推送（没有推送选项：本地已经领先上游 1 个提交' in run.stdout
    assert _remote_head(tmp_path) == pushed_before


def test_with_unpushed_local_commits_git_mode_push_exits_3(tmp_path: Path):
    root = _repository(tmp_path)
    _git(root, tmp_path, 'commit', '-q', '--allow-empty', '-m', 'local only')
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git, run.matter['exit_code']) == (3, 'not_offered', '3')
    assert '`--git-mode push` 没有执行：没有推送选项：' in run.stdout
    assert _head(root, tmp_path) == head


@pytest.mark.parametrize(
    'reader',
    [lambda stream: stream, host_module._Lines],  # pyright: ignore[reportPrivateUsage]
    ids=['the_stream', 'the_hosts_reader'],
)
def test_a_buffered_answer_typed_before_the_question_does_not_count(
    reader: Callable[[TextIO], object],
):
    master, slave = os.openpty()
    try:
        os.write(master, b'1\n')

        def waiting() -> int:
            return struct.unpack('i', fcntl.ioctl(slave, termios.FIONREAD, b'\0' * 4))[0]

        deadline = time.monotonic() + 5
        while waiting() != 2 and time.monotonic() < deadline:
            time.sleep(0.005)

        def answer_after_the_flush() -> None:
            deadline = time.monotonic() + 5
            while waiting() != 0 and time.monotonic() < deadline:
                time.sleep(0.005)
            os.write(master, b'2\n')

        typist = threading.Thread(target=answer_after_the_flush)
        typist.start()
        with open(slave, encoding='utf-8', closefd=False) as stdin:
            answer = ask(cast(TextIO, reader(stdin)), StringIO(), 'summary', Offer((), None, ()))
        typist.join()
    finally:
        os.close(master)
        os.close(slave)

    assert answer == '2'


# ---- --git-mode ----


def test_git_mode_push_does_not_ask_and_pushes(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git, run.matter['exit_code']) == (0, 'pushed', '0')
    assert '选哪个' not in run.stderr
    assert _remote_head(tmp_path) == _head(root, tmp_path)
    assert '选择：`--git-mode push`。' in run.stdout


def test_git_mode_commit_does_not_ask_and_does_not_push(tmp_path: Path):
    root = _repository(tmp_path)
    pushed_before = _remote_head(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert (run.code, run.git) == (0, 'committed')
    assert '选哪个' not in run.stderr
    assert _remote_head(tmp_path) == pushed_before != _head(root, tmp_path)


def test_git_mode_manual_declines_without_asking(tmp_path: Path):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='manual', stdin='1\n')

    assert (run.code, run.git) == (0, 'declined')
    assert '选哪个' not in run.stderr
    assert _head(root, tmp_path) == head


@pytest.mark.parametrize('mode', ['commit', 'push'])
def test_git_mode_on_an_unverified_run_exits_3(tmp_path: Path, mode: str):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(verdict=None), root=root, git_mode=mode)

    assert (run.matter['status'], run.matter['reviewer_verdict']) == ('unverified', 'unverified')
    assert (run.code, run.git, run.matter['exit_code']) == (3, 'not_offered', '3')
    assert f'`--git-mode {mode}` 没有执行：提交的前置条件不满足：' in run.stdout
    assert '- Reviewer 结论是 unverified，不是 pass' in run.stdout
    assert _head(root, tmp_path) == head


def test_git_mode_manual_on_an_unverified_run_names_what_is_missing(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(verdict=None), root=root, git_mode='manual')

    assert (run.code, run.git, run.matter['exit_code']) == (0, 'not_offered', '0')
    assert '- Reviewer 结论是 unverified，不是 pass' in run.stdout


def test_git_mode_push_with_a_failing_verify_exits_1_and_never_says_3(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push', verify=['true', 'false'])

    assert run.matter['status'] == 'verification_failed'
    assert (run.code, run.git, run.matter['exit_code']) == (1, 'not_offered', '1')
    assert '退出 3' not in run.stdout
    assert '- 宿主复核没有全过' in run.stdout


def test_without_verify_a_reviewer_pass_is_not_enough(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root, stdin='1\n', verify=None)

    assert (run.matter['host_verify'], run.matter['reviewer_verdict']) == ('not_configured', 'pass')
    assert (run.code, run.git) == (0, 'not_offered')
    assert '- 没有配置 workspace.verify：宿主没有复核' in run.stdout


@pytest.mark.parametrize('mode', ['commit', 'push', 'manual'])
def test_git_mode_requires_handoff_and_refuses_no_write(tmp_path: Path, mode: str):
    stderr = StringIO()
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr)
    host = RuntimeCliHost(io=io, model_factory=lambda _: ScriptedLLM([]), workspace_root=tmp_path)
    try:
        alone = main(['run', '--git-mode', mode, 'x'], host=host, io=io)
        read_only = main(
            ['run', '--handoff', '--no-write', '--git-mode', mode, 'x'], host=host, io=io
        )
    finally:
        host.close()

    assert (alone, read_only) == (2, 2)
    assert '加上 --handoff' in stderr.getvalue()
    assert '只读 run 没有改动，不做 git 收尾' in stderr.getvalue()


# ---- 前置条件 ----


def test_a_run_without_a_reviewer_pass_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(verdict=None), root=root, stdin='1\n')

    assert (run.code, run.git) == (0, 'not_offered')
    assert '选哪个' not in run.stderr
    assert '- Reviewer 结论是 unverified，不是 pass' in run.stdout


def test_a_run_without_changes_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(_call('b', 'Bash', command='true')), root=root, stdin='1\n')

    assert run.matter['status'] == 'verified'
    assert (run.code, run.git) == (0, 'not_offered')
    assert '- 这次 run 没有改动' in run.stdout


def test_a_detached_head_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)
    _git(root, tmp_path, 'checkout', '-q', '--detach')

    run = _run(tmp_path, _script(), root=root, stdin='2\n')

    assert run.git == 'not_offered'
    assert '- 不在分支上（游离 HEAD）' in run.stdout


def test_a_merge_in_progress_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)
    (root / '.git' / 'MERGE_HEAD').write_text(_head(root, tmp_path) + '\n', encoding='utf-8')

    run = _run(tmp_path, _script(), root=root, stdin='2\n')

    assert run.git == 'not_offered'
    assert '- 有进行中的 merge' in run.stdout


def test_a_workspace_below_the_top_level_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(_write('new.py')), root=root / 'src', stdin='2\n')

    assert run.matter['status'] == 'verified'
    assert run.git == 'not_offered'
    assert '- 工作区根不是 git 顶层目录' in run.stdout


def test_a_file_changed_before_the_run_and_again_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'src' / 'keep.py').write_text('KEEP = 2\n', encoding='utf-8')

    run = _run(tmp_path, _script(_write('src/keep.py', 'KEEP = 3\n')), root=root, stdin='2\n')

    assert run.git == 'not_offered'
    assert '- 有运行前就改过、这次又被改的文件' in run.stdout


def test_a_file_ignored_before_the_run_showing_up_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)
    (root / '.env').write_text('LOCAL=1\n', encoding='utf-8')

    run = _run(tmp_path, _script(_write('.gitignore', '# nothing\n')), root=root, stdin='2\n')

    assert run.git == 'not_offered'
    assert '- 运行前被忽略的文件出现在改动里：.env' in run.stdout
    assert '?? .env' in _status(root, tmp_path)


@pytest.mark.parametrize(
    'meddle',
    [
        pytest.param(
            lambda root, tmp: _git(root, tmp, 'config', 'core.fsmonitor', 'touch pwned'),
            id='fsmonitor',
        ),
        pytest.param(
            lambda root, tmp: _git(root, tmp, 'remote', 'set-url', 'origin', str(tmp / 'x.git')),
            id='remote-url',
        ),
        pytest.param(
            lambda root, tmp: (tmp / 'home' / 'hooks' / 'pre-commit').write_text('#!/bin/sh\n'),
            id='global-hook',
        ),
        pytest.param(
            lambda root, tmp: _git(root, tmp, 'commit', '-q', '--allow-empty', '-m', 'sneaky'),
            id='head',
        ),
    ],
)
def test_a_repository_changed_during_the_run_is_not_offered_anything(
    tmp_path: Path, meddle: Callable[[Path, Path], object]
):
    root = _repository(tmp_path)
    llm = _Meddling(_script(), at=1, meddle=lambda: meddle(root, tmp_path))

    run = _run(tmp_path, llm, root=root, git_mode='push')

    assert run.matter['status'] == 'verified'
    assert (run.code, run.git) == (3, 'not_offered')
    assert '仓库的完整性快照与 run 开始前不一致' in run.stdout
    assert not (root / 'pwned').exists()


@pytest.mark.skipif(os.geteuid() == 0, reason='root 不受文件权限约束')
def test_a_failed_pre_run_snapshot_is_not_offered_anything(tmp_path: Path):
    root = _repository(tmp_path)
    (root / '.locked').write_text('x\n', encoding='utf-8')
    (root / '.locked').chmod(0)
    calls = (_call('b', 'Bash', command='rm -f .locked'), _write('src/new.py'))

    run = _run(tmp_path, _script(*calls), root=root, git_mode='commit')

    assert run.matter['status'] == 'verified'
    assert (run.code, run.git) == (3, 'not_offered')
    assert '- 运行前的快照没取到' in run.stdout


def test_a_mode_change_while_the_question_is_pending_is_not_committed(tmp_path: Path):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    def chmod() -> None:
        (root / 'src' / 'new.py').chmod(0o755)

    run = _run(tmp_path, _script(), root=root, stdin=_Answering('2\n', chmod))

    assert (run.code, run.git) == (1, 'failed')
    assert '复核之后这些文件又变了，没有提交：src/new.py' in run.stdout
    assert _head(root, tmp_path) == head


def test_a_file_changed_while_the_question_is_pending_is_not_committed(tmp_path: Path):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    def edit() -> None:
        (root / 'src' / 'new.py').write_text('NEW = 2\n', encoding='utf-8')

    run = _run(tmp_path, _script(), root=root, stdin=_Answering('2\n', edit))

    assert (run.code, run.git, run.matter['exit_code']) == (1, 'failed', '1')
    assert '复核之后这些文件又变了，没有提交：src/new.py' in run.stdout
    assert _head(root, tmp_path) == head


# ---- 提交什么 ----


def test_only_this_runs_paths_are_committed(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'staged.txt').write_text('staged by the human\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'staged.txt')
    (root / 'scratch.txt').write_text('untracked\n', encoding='utf-8')
    (root / '.env').write_text('LOCAL=1\n', encoding='utf-8')

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert run.git == 'committed'
    assert _committed_paths(root, tmp_path) == {'src/new.py'}
    assert _status(root, tmp_path) == ['?? scratch.txt', 'A  staged.txt']


def test_odd_file_names_are_committed_as_themselves_and_nothing_runs(tmp_path: Path):
    root = _repository(tmp_path)
    names = [
        'a;touch pwned1',
        '$(touch pwned2)',
        '`touch pwned3`',
        'line\nbreak.txt',
        '-rf.txt',
        '*',
        '[ab].py',
        ':(top)x',
        ':!x',
    ]
    for decoy in ('a.py', 'b.py', 'x', 'scratch.txt'):
        (root / decoy).write_text('decoy\n', encoding='utf-8')
    calls = [_write(name, f'{index}\n', f'w{index}') for index, name in enumerate(names)]

    run = _run(tmp_path, _script(*calls), root=root, stdin='2\n')

    assert run.git == 'committed'
    assert _committed_paths(root, tmp_path) == set(names)
    assert {'?? a.py', '?? b.py', '?? scratch.txt', '?? x'} <= set(_status(root, tmp_path))
    assert not any(path.name.startswith('pwned') for path in root.iterdir())
    assert '- `??` `+1 −0` line\\nbreak.txt' in run.stderr


def test_a_protected_path_change_is_listed_and_left_to_the_human(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(_write('.gitignore', '.env\n*.log\n')), root=root, stdin='2\n')

    assert run.matter['protected_changed'] == '[".gitignore"]'
    assert '！！受保护路径有改动' in run.stderr
    assert run.git == 'committed'
    assert _committed_paths(root, tmp_path) == {'.gitignore'}
    assert '受保护路径有改动，提交前已经在询问里逐个列出：' in run.stdout


@pytest.mark.parametrize('mode', ['commit', 'push'])
def test_a_protected_path_change_makes_git_mode_exit_3(tmp_path: Path, mode: str):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(_write('.gitignore', '.env\n*.log\n')), root=root, git_mode=mode)

    assert (run.code, run.git) == (3, 'not_offered')
    assert f'`--git-mode {mode}` 没有执行：受保护路径有改动' in run.stdout
    assert '- .gitignore' in run.stdout
    assert _head(root, tmp_path) == head


def test_a_secret_in_the_change_refuses_the_commit_without_printing_it(tmp_path: Path):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)
    content = f'# config\nTOKEN = "{_SECRET}"\n'

    run = _run(tmp_path, _script(_write('src/config.py', content)), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert 'src/config.py:2（secret.env_value）' in run.stdout
    assert _SECRET not in run.stdout + run.stderr
    assert _head(root, tmp_path) == head


def test_a_secret_in_a_modified_file_is_reported_at_its_line(tmp_path: Path):
    root = _repository(tmp_path)
    content = f'KEEP = 1\n\nTOKEN = "{_SECRET}"\n'
    edit = _call('e', 'Edit', path='src/keep.py', old_string='KEEP = 1\n', new_string=content)

    run = _run(tmp_path, _script(edit), root=root, git_mode='commit')

    assert run.git == 'failed'
    assert 'src/keep.py:3（secret.env_value）' in run.stdout


def test_a_new_symlink_whose_target_text_is_a_secret_is_refused(tmp_path: Path):
    root = _repository(tmp_path)
    link = _call('b', 'Bash', command=f'ln -s {_SECRET} leak')

    run = _run(tmp_path, _script(_write('src/new.py'), link), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert 'leak:1（secret.env_value）' in run.stdout


@pytest.mark.parametrize(
    ('title', 'secret'),
    [
        ('rotate {secret}', _SECRET),
        ('x' * 60 + '{secret}', _SECRET),
        ('Rotate the deploy key; new value {secret}', _github_token()),
    ],
    ids=['env-value', 'env-value-cut-mid-secret', 'format-rule-cut-mid-secret'],
)
def test_a_secret_in_the_commit_title_never_reaches_the_commit(
    tmp_path: Path, title: str, secret: str
):
    root = _repository(tmp_path)
    answer = 'done\ncommit_title: ' + title.format(secret=secret)

    run = _run(tmp_path, _script(answer=answer), root=root, git_mode='commit')

    assert run.git == 'committed'
    first = _message(root, tmp_path).split('\n', 1)[0]
    assert '[REDACTED:' in first
    assert secret[4:12] not in first


def test_a_title_starting_with_a_hash_is_kept_verbatim(tmp_path: Path):
    root = _repository(tmp_path)
    with _global_config(tmp_path).open('a', encoding='utf-8') as config:
        config.write('[commit]\n\tcleanup = strip\n')

    run = _run(
        tmp_path, _script(answer='done\ncommit_title: #42 fix it'), root=root, git_mode='commit'
    )

    assert run.git == 'committed'
    assert _message(root, tmp_path) == _MESSAGE.replace('add new', '#42 fix it')


def test_a_secret_in_a_verify_command_never_reaches_the_commit_message(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(
        tmp_path, _script(), root=root, git_mode='commit', verify=[f'SERVICE_TOKEN={_SECRET} true']
    )

    assert run.git == 'committed'
    message = _message(root, tmp_path)
    assert '- 通过：SERVICE_TOKEN=[REDACTED:secret.env_value] true —— passed' in message
    assert _SECRET not in message


def test_the_commit_runs_with_trace2_switched_off(tmp_path: Path):
    root = _repository(tmp_path)
    trace = tmp_path / 'trace.json'
    with _global_config(tmp_path).open('a', encoding='utf-8') as config:
        config.write(f'[trace2]\n\teventTarget = {trace}\n')

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    events = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
    started = [event['argv'] for event in events if event.get('event') == 'start']
    assert run.git == 'committed'
    assert any('status' in argv for argv in started)
    assert not any('commit' in argv for argv in started)


@pytest.mark.parametrize(
    'hook',
    [
        'echo extra > extra.txt && git add extra.txt',
        "printf 'NEW = 9\\n' > src/new.py && git add src/new.py",
    ],
    ids=['adds-a-file', 'rewrites-the-file'],
)
def test_a_commit_hook_that_changes_the_commit_is_reported_and_not_pushed(
    tmp_path: Path, hook: str
):
    root = _repository(tmp_path)
    path = tmp_path / 'home' / 'hooks' / 'pre-commit'
    path.write_text(f'#!/bin/sh\n{hook}\n', encoding='utf-8')
    path.chmod(0o755)
    pushed_before = _remote_head(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (1, 'failed')
    assert '提交改到的路径或内容与复核时的不一致' in run.stdout
    assert f'提交：`{_head(root, tmp_path)}`' in run.stdout
    assert _remote_head(tmp_path) == pushed_before


def test_the_commit_message_is_exactly_the_generated_one(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert run.git == 'committed'
    assert _message(root, tmp_path) == _MESSAGE
    author = _git(root, tmp_path, 'log', '-1', '--format=%an <%ae>').strip()
    assert author == 'Syndica Test <test@example.com>'


def test_the_handoff_records_the_message_a_commit_msg_hook_left(tmp_path: Path):
    root = _repository(tmp_path)
    hook = tmp_path / 'home' / 'hooks' / 'commit-msg'
    hook.write_text('#!/bin/sh\nprintf "Hooked-by: test\\n" >> "$1"\n', encoding='utf-8')
    hook.chmod(0o755)

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert run.git == 'committed'
    assert _message(root, tmp_path) == _MESSAGE + 'Hooked-by: test\n'
    assert '> Hooked-by: test' in run.stdout


def test_without_a_commit_title_the_goal_names_the_commit(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(answer='done'), root=root, git_mode='commit')

    assert run.git == 'committed'
    assert _message(root, tmp_path).split('\n', 1)[0] == 'work on it'


def test_a_file_written_with_mixed_case_is_not_flagged_as_bash_made(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(_write('src/New.py')), root=root)

    assert '- `??` `+1 −0` src/New.py\n' in run.stderr


def test_the_commit_body_lists_every_verify_command_in_order(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(
        tmp_path, _script(), root=root, git_mode='commit', verify=['echo c', 'true', 'echo a']
    )

    assert run.git == 'committed'
    assert _message(root, tmp_path).split('宿主复核：\n', 1)[1] == (
        '- 通过：echo c —— passed\n- 通过：true —— passed\n- 通过：echo a —— passed\n'
    )


def test_the_cli_drops_typed_ahead_input_before_reading_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    flushed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        git_finish.termios, 'tcflush', lambda fd, queue: flushed.append((fd, queue))
    )
    root = _repository(tmp_path)
    master, slave = os.openpty()
    try:
        os.write(master, b'3\n')
        with open(slave, encoding='utf-8', closefd=False) as stdin:
            run = _run(tmp_path, _script(), root=root, stdin=stdin)
    finally:
        os.close(master)
        os.close(slave)

    assert run.git == 'declined'
    assert flushed == [(slave, termios.TCIFLUSH)]


def test_the_summary_marks_changes_not_made_by_write_or_edit(tmp_path: Path):
    root = _repository(tmp_path)
    calls = (_write('src/new.py'), _call('b', 'Bash', command='printf x > via_bash.txt'))

    run = _run(tmp_path, _script(*calls), root=root)

    assert '- `??` `+1 −0` src/new.py\n' in run.stderr
    assert '- `??` `+1 −0` via_bash.txt（不是 Write / Edit 产生的）' in run.stderr


# ---- 推送 ----


def test_a_rejected_non_fast_forward_push_is_push_failed_and_never_forced(tmp_path: Path):
    root = _repository(tmp_path)
    other = tmp_path / 'other'
    _git(tmp_path, tmp_path, 'clone', '-q', str(tmp_path / 'remote.git'), str(other))
    _git(other, tmp_path, 'commit', '-q', '--allow-empty', '-m', 'someone else')
    _git(other, tmp_path, 'push', '-q', 'origin', 'main')
    theirs = _remote_head(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git, run.matter['exit_code']) == (1, 'push_failed', '1')
    assert '期望推送是快进' in run.stdout
    assert '`git pull --rebase`' in run.stdout
    assert _remote_head(tmp_path) == theirs
    assert _committed_paths(root, tmp_path) == {'src/new.py'}


def test_push_goes_to_the_snapshots_remote_and_upstream_branch(tmp_path: Path):
    root = _repository(tmp_path, upstream=False)
    hub = tmp_path / 'hub.git'
    _git(tmp_path, tmp_path, 'init', '-q', '--bare', str(hub))
    _git(root, tmp_path, 'remote', 'add', 'hub', str(hub))
    _git(root, tmp_path, 'push', '-q', 'hub', 'main:trunk')
    _git(root, tmp_path, 'checkout', '-q', '-b', 'work', '--track', 'hub/trunk')

    run = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert f'git 收尾：分支 work → 上游 hub/trunk（推送到 {hub}）' in run.stderr
    assert run.git == 'pushed'
    assert _git(hub, tmp_path, 'rev-parse', 'trunk').strip() == _head(root, tmp_path)
    assert _git(hub, tmp_path, 'branch', '--list', 'work') == ''


def test_an_ssh_authentication_failure_is_named(tmp_path: Path):
    root = _repository(tmp_path, upstream=False)
    stub = tmp_path / 'ssh-stub'
    stub.write_text(
        '#!/bin/sh\necho "git@example.invalid: Permission denied (publickey)." >&2\nexit 255\n',
        encoding='utf-8',
    )
    stub.chmod(0o755)
    _git(root, tmp_path, 'remote', 'add', 'origin', 'git@example.invalid:team/repo.git')
    _git(root, tmp_path, 'config', 'branch.main.remote', 'origin')
    _git(root, tmp_path, 'config', 'branch.main.merge', 'refs/heads/main')
    _git(root, tmp_path, 'config', 'core.sshCommand', str(stub))
    _git(root, tmp_path, 'update-ref', 'refs/remotes/origin/main', 'HEAD')

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (1, 'push_failed')
    assert '期望推送通过远端认证，实际被拒' in run.stdout
    assert 'Permission denied (publickey)' in run.stdout


def test_a_non_fast_forward_after_a_fetch_is_named(tmp_path: Path):
    root = _repository(tmp_path)
    other = tmp_path / 'other'
    _git(tmp_path, tmp_path, 'clone', '-q', str(tmp_path / 'remote.git'), str(other))
    _git(other, tmp_path, 'commit', '-q', '--allow-empty', '-m', 'someone else')
    _git(other, tmp_path, 'push', '-q', 'origin', 'main')
    _git(root, tmp_path, 'fetch', '-q')

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (1, 'push_failed')
    assert '(non-fast-forward)' in run.stdout
    assert '期望推送是快进' in run.stdout


def test_the_push_gets_its_own_longer_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[float] = []
    original = HostGit.attempt

    def recording(
        self: HostGit, *args: str, timeout: float = 60.0
    ) -> subprocess.CompletedProcess[bytes]:
        if 'push' in args:
            seen.append(timeout)
        return original(self, *args, timeout=timeout)

    monkeypatch.setattr(HostGit, 'attempt', recording)
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert run.git == 'pushed'
    assert seen == [120.0]


def test_an_upstream_that_was_never_fetched_means_no_push(tmp_path: Path):
    root = _repository(tmp_path)
    _git(root, tmp_path, 'update-ref', '-d', 'refs/remotes/origin/main')

    run = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert '（没有推送选项：算不出本地领先上游几个提交）' in run.stderr
    assert run.git == 'declined'


def test_a_push_to_a_missing_remote_repository_says_so(tmp_path: Path):
    root = _repository(tmp_path)
    shutil.rmtree(tmp_path / 'remote.git')

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (1, 'push_failed')
    assert '期望推送到一个存在的远端仓库，实际找不到它' in run.stdout
    assert '`git remote -v`' in run.stdout


def test_a_push_declined_by_a_remote_hook_is_push_failed(tmp_path: Path):
    root = _repository(tmp_path)
    remote = tmp_path / 'remote.git'
    hook = remote / 'hooks' / 'pre-receive'
    hook.write_text('#!/bin/sh\necho "no pushes today" >&2\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    _git(remote, tmp_path, 'config', 'core.hooksPath', str(remote / 'hooks'))

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (1, 'push_failed')
    assert '期望远端接受推送，实际远端拒绝了' in run.stdout
    assert 'no pushes today' in run.stdout


# ---- 钩子与配置里的程序 ----


def test_commit_hooks_run_without_the_hosts_secrets_or_ssh_agent(tmp_path: Path):
    root = _repository(tmp_path)
    seen = tmp_path / 'seen'
    _hook(tmp_path, 'pre-commit', f'echo "token=$SERVICE_TOKEN ssh=$SSH_AUTH_SOCK" > {seen}')

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert run.git == 'committed'
    assert seen.read_text(encoding='utf-8') == 'token= ssh=\n'
    author = _git(root, tmp_path, 'log', '-1', '--format=%an <%ae>').strip()
    assert author == 'Syndica Test <test@example.com>'


def test_a_husky_style_hook_that_runs_worktree_code_gets_no_host_secrets(tmp_path: Path):
    root = _repository(tmp_path)
    (root / '.husky' / '_').mkdir(parents=True)
    stub = root / '.husky' / '_' / 'pre-commit'
    stub.write_text('#!/bin/sh\n. "$(dirname "$0")/../pre-commit"\n', encoding='utf-8')
    stub.chmod(0o755)
    (root / '.husky' / 'pre-commit').write_text('true\n', encoding='utf-8')
    _git(root, tmp_path, 'add', '.')
    _git(root, tmp_path, 'commit', '-q', '-m', 'husky')
    _git(root, tmp_path, 'push', '-q')
    _git(root, tmp_path, 'config', 'core.hooksPath', '.husky/_')
    leak = tmp_path / 'leak'
    edit = _write('.husky/pre-commit', f'echo "token=$SERVICE_TOKEN" > {leak}\n')

    run = _run(tmp_path, _script(edit), root=root, git_mode='commit')

    assert run.git == 'committed'
    assert leak.read_text(encoding='utf-8') == 'token=\n'


def test_a_push_time_hook_means_no_push_option(tmp_path: Path):
    root = _repository(tmp_path)
    ran = tmp_path / 'ran'
    _hook(tmp_path, 'pre-push', f'touch {ran}')

    asked = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert '1. 提交并推送' not in asked.stderr
    assert '推送时会在本地运行的钩子（pre-push）' in asked.stderr
    assert asked.git == 'declined'
    assert not ran.exists()


def test_a_push_time_hook_named_in_another_case_still_means_no_push_option(tmp_path: Path):
    root = _repository(tmp_path)
    _hook(tmp_path, 'Pre-Push', 'exit 0')

    asked = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert '1. 提交并推送' not in asked.stderr
    assert '推送时会在本地运行的钩子（Pre-Push）' in asked.stderr
    assert asked.git == 'declined'


def test_a_push_time_hook_without_the_executable_bit_leaves_the_push_option(tmp_path: Path):
    root = _repository(tmp_path)
    ran = tmp_path / 'ran'
    _hook(tmp_path, 'pre-push', f'touch {ran}').chmod(0o644)

    run = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert run.git == 'pushed'
    assert not ran.exists()


def test_the_summary_lists_the_executable_commit_hooks_of_the_real_hooks_directory(
    tmp_path: Path,
):
    root = _repository(tmp_path)
    _hook(tmp_path, 'pre-commit', 'exit 0')
    _hook(tmp_path, 'post-commit', 'exit 0').chmod(0o644)
    _hook(tmp_path, 'pre-push', 'exit 0')

    run = _run(tmp_path, _script(), root=root, stdin='3\n')

    line = '提交时会运行钩子（在 Agent 的环境里，拿不到 ssh agent 与密钥）：'
    assert f'{line}pre-commit' in run.stderr.splitlines()


def test_the_commit_uses_the_global_config_found_under_xdg_config_home(tmp_path: Path):
    root = _repository(tmp_path)
    hooks = tmp_path / 'xdg-hooks'
    hooks.mkdir()
    marker = tmp_path / 'xdg-hook-ran'
    hook = hooks / 'commit-msg'
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n', encoding='utf-8')
    hook.chmod(0o755)
    (tmp_path / 'xdg' / 'git').mkdir(parents=True)
    (tmp_path / 'xdg' / 'git' / 'config').write_text(
        f'[user]\n\tname = Xdg User\n\temail = xdg@example.com\n[core]\n\thooksPath = {hooks}\n',
        encoding='utf-8',
    )
    environ = {key: value for key, value in _env(tmp_path).items() if key != 'GIT_CONFIG_GLOBAL'}
    environ['XDG_CONFIG_HOME'] = str(tmp_path / 'xdg')

    run = _run(tmp_path, _script(), root=root, git_mode='commit', environ=environ)

    assert run.git == 'committed'
    assert marker.exists()
    assert _git(root, tmp_path, 'log', '-1', '--format=%an').strip() == 'Xdg User'


def test_a_push_time_hook_makes_git_mode_push_exit_3(tmp_path: Path):
    root = _repository(tmp_path)
    _hook(tmp_path, 'pre-push', 'exit 0')
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (3, 'not_offered')
    assert _head(root, tmp_path) == head


def test_an_included_config_file_changed_during_the_run_blocks_the_finish(tmp_path: Path):
    root = _with_tracked(tmp_path, {'shared.gitconfig': '# team settings\n'})
    _git(root, tmp_path, 'config', 'include.path', '../shared.gitconfig')
    evil = tmp_path / 'evil.git'
    _git(tmp_path, tmp_path, 'init', '-q', '--bare', str(evil))
    content = f'[url "{evil}"]\n\tpushInsteadOf = {tmp_path / "remote.git"}\n'

    run = _run(tmp_path, _script(_write('shared.gitconfig', content)), root=root, git_mode='push')

    assert (run.code, run.git) == (3, 'not_offered')
    assert 'git 配置（`git config --list` 的结果）' in run.stdout
    assert _git(evil, tmp_path, 'branch', '--list') == ''
    assert '先用编辑器或 `cat` 看 `.git/config`' in run.stdout


def test_a_forged_upstream_ref_blocks_the_finish(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'local.txt').write_text('not ready to publish\n', encoding='utf-8')
    _git(root, tmp_path, 'add', 'local.txt')
    _git(root, tmp_path, 'commit', '-q', '-m', 'local only')
    llm = _Meddling(
        _script(),
        at=1,
        meddle=lambda: _git(root, tmp_path, 'update-ref', 'refs/remotes/origin/main', 'HEAD'),
    )

    run = _run(tmp_path, llm, root=root, git_mode='push')

    assert (run.code, run.git) == (3, 'not_offered')
    assert '上游分支在本地的位置' in run.stdout
    log = _git(tmp_path / 'remote.git', tmp_path, 'log', '--format=%s', 'main')
    assert 'local only' not in log


def test_a_commit_made_while_the_question_is_pending_is_not_pushed(tmp_path: Path):
    root = _repository(tmp_path)

    def sneak() -> None:
        (root / 'backdoor.py').write_text('import os\n', encoding='utf-8')
        _git(root, tmp_path, 'add', 'backdoor.py')
        _git(root, tmp_path, 'commit', '-q', '-m', 'sneaky')

    run = _run(tmp_path, _script(), root=root, stdin=_Answering('1\n', sneak))

    assert (run.code, run.git) == (1, 'failed')
    assert '问过之后、提交之前，仓库变了（HEAD' in run.stdout
    assert 'sneaky' not in _git(tmp_path / 'remote.git', tmp_path, 'log', '--format=%s', 'main')


def test_a_hook_planted_while_the_question_is_pending_never_runs(tmp_path: Path):
    root = _repository(tmp_path)
    leak = tmp_path / 'leak'

    def plant() -> None:
        _hook(tmp_path, 'pre-commit', f'echo planted > {leak}')

    run = _run(tmp_path, _script(), root=root, stdin=_Answering('2\n', plant))

    assert (run.code, run.git) == (1, 'failed')
    assert '钩子 pre-commit' in run.stdout
    assert not leak.exists()


def test_a_post_commit_hook_that_redirects_the_push_stops_the_push(tmp_path: Path):
    root = _repository(tmp_path)
    evil = tmp_path / 'evil.git'
    _git(tmp_path, tmp_path, 'init', '-q', '--bare', str(evil))
    _hook(tmp_path, 'post-commit', f'git config remote.origin.pushurl {evil}')
    pushed_before = _remote_head(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (1, 'push_failed')
    assert '提交之后、推送之前，仓库变了（推送地址' in run.stdout
    assert f'提交：`{_head(root, tmp_path)}`' in run.stdout
    assert _git(evil, tmp_path, 'branch', '--list') == ''
    assert _remote_head(tmp_path) == pushed_before


def test_a_slow_post_commit_hook_reports_the_commit_that_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(git_finish, 'LOCAL_TIMEOUT', _HOOK_STARTS_WITHIN)
    monkeypatch.setattr(git_finish, '_STOP_GRACE', 0.5)
    root = _repository(tmp_path)
    pid = tmp_path / 'pid'
    _hook(tmp_path, 'post-commit', f'echo $$ > {pid}\nsleep 30')
    pushed_before = _remote_head(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert (run.code, run.git) == (1, 'failed')
    assert f'提交：`{_head(root, tmp_path)}`' in run.stdout
    assert '提交已经在本地：没有推送' in run.stdout
    assert _committed_paths(root, tmp_path) == {'src/new.py'}
    assert _remote_head(tmp_path) == pushed_before
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text(encoding='utf-8')), 0)


def test_a_slow_pre_commit_hook_leaves_no_lock_and_nothing_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(git_finish, 'LOCAL_TIMEOUT', 2.0)
    monkeypatch.setattr(git_finish, '_STOP_GRACE', 0.5)
    root = _repository(tmp_path)
    head = _head(root, tmp_path)
    _hook(tmp_path, 'pre-commit', 'sleep 30')

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert '在 2 秒内没有结束' in run.stdout
    assert _head(root, tmp_path) == head
    assert not (root / '.git' / 'index.lock').exists()
    assert '?? src/new.py' in _status(root, tmp_path)


@pytest.mark.parametrize('change', ['rewrite', 'chmod'])
def test_changing_an_existing_hook_during_the_run_blocks_the_finish(tmp_path: Path, change: str):
    root = _repository(tmp_path)
    hook = tmp_path / 'home' / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    hook.chmod(0o644 if change == 'chmod' else 0o755)

    def meddle() -> None:
        if change == 'chmod':
            hook.chmod(0o755)
        else:
            hook.write_text('#!/bin/sh\nexit 0 # changed\n', encoding='utf-8')

    run = _run(tmp_path, _Meddling(_script(), at=1, meddle=meddle), root=root, git_mode='commit')

    assert (run.code, run.git) == (3, 'not_offered')
    assert '钩子 pre-commit' in run.stdout


def test_a_configured_fsmonitor_never_runs_during_the_finish(tmp_path: Path):
    root = _repository(tmp_path)
    pwned = tmp_path / 'pwned'
    _git(root, tmp_path, 'config', 'core.fsmonitor', f'touch {pwned}; false')

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert not pwned.exists()
    assert run.git == 'committed'


def test_a_filter_selected_by_the_agents_gitattributes_never_runs(tmp_path: Path):
    root = _repository(tmp_path)
    filtered = tmp_path / 'filtered'
    _git(root, tmp_path, 'config', 'filter.evil.clean', f'touch {filtered}; cat')
    calls = (_write('.gitattributes', '* filter=evil\n'), _write('src/new.py', 'NEW = 1\n', 'w2'))

    run = _run(tmp_path, _script(*calls), root=root, stdin='2\n')

    assert run.git == 'committed'
    assert not filtered.exists()


def test_a_hook_that_ignores_sigterm_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(git_finish, 'LOCAL_TIMEOUT', _HOOK_STARTS_WITHIN)
    monkeypatch.setattr(git_finish, '_STOP_GRACE', 0.5)
    root = _repository(tmp_path)
    pid = tmp_path / 'pid'
    _hook(tmp_path, 'pre-commit', f"trap '' TERM\necho $$ > {pid}\nsleep 120")

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text(encoding='utf-8')), 0)


def test_a_timed_out_hook_gets_sigterm_before_it_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(git_finish, 'LOCAL_TIMEOUT', _HOOK_STARTS_WITHIN)
    monkeypatch.setattr(git_finish, '_STOP_GRACE', 5.0)
    root = _repository(tmp_path)
    marker = tmp_path / 'marker'
    _hook(
        tmp_path,
        'pre-commit',
        f"trap 'echo term > {marker}; exit 1' TERM\nwhile :; do sleep 1; done",
    )

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert marker.read_text(encoding='utf-8') == 'term\n'


def test_a_rejecting_pre_commit_hook_unstages_the_new_files(tmp_path: Path):
    root = _repository(tmp_path)
    _hook(tmp_path, 'pre-commit', 'echo "lint failed" >&2\nexit 1')

    run = _run(tmp_path, _script(), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert 'lint failed' in run.stdout
    assert '为提交暂存的新文件已经撤回' in run.stdout
    assert '?? src/new.py' in _status(root, tmp_path)


# ---- 提交闸 ----


def test_an_added_line_that_starts_with_plus_signs_is_still_scanned(tmp_path: Path):
    root = _repository(tmp_path)
    edit = _call(
        'e',
        'Edit',
        path='README.md',
        old_string='# project\n',
        new_string=f'# project\n++ deploy token: {_SECRET}\n',
    )

    run = _run(tmp_path, _script(edit), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert 'README.md:2（secret.env_value）' in run.stdout


@pytest.mark.parametrize('attribute', ['*.cfg -diff\n', '*.cfg binary\n'])
def test_diff_attributes_do_not_hide_a_modified_file_from_the_scan(tmp_path: Path, attribute: str):
    root = _with_tracked(tmp_path, {'app.cfg': 'A = 1\n', '.gitattributes': attribute})
    edit = _call(
        'e', 'Edit', path='app.cfg', old_string='A = 1\n', new_string=f'A = 1\nT = "{_SECRET}"\n'
    )

    run = _run(tmp_path, _script(edit), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert 'app.cfg:2（secret.env_value）' in run.stdout


def test_a_nul_byte_does_not_exempt_a_new_file_from_the_scan(tmp_path: Path):
    root = _repository(tmp_path)
    content = f'TOKEN = "{_SECRET}"\n\x00\n'

    run = _run(tmp_path, _script(_write('notes.txt', content)), root=root, git_mode='commit')

    assert (run.code, run.git) == (1, 'failed')
    assert 'notes.txt:1（secret.env_value）' in run.stdout


def test_the_question_never_prints_a_secret(tmp_path: Path):
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(_write(f'cache-{_SECRET}.txt', 'x\n')), root=root, stdin='3\n')

    assert run.git == 'declined'
    assert '- `??` `+1 −0` cache-[REDACTED:secret.env_value].txt' in run.stderr
    assert _SECRET not in run.stderr


def test_a_secret_in_a_file_name_refuses_the_commit_without_printing_it(tmp_path: Path):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    run = _run(
        tmp_path, _script(_write(f'cache-{_SECRET}.txt', 'x\n')), root=root, git_mode='commit'
    )

    assert (run.code, run.git) == (1, 'failed')
    assert '文件名 cache-[REDACTED:secret.env_value].txt（secret.env_value）' in run.stdout
    assert _SECRET not in run.stdout + run.stderr
    assert _head(root, tmp_path) == head


# ---- 推送与提交的细节 ----


def test_a_change_made_before_the_run_means_no_push(tmp_path: Path):
    root = _repository(tmp_path)
    (root / 'src' / 'keep.py').write_text('KEEP = 1\nVALUE = 2\n', encoding='utf-8')

    asked = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert '运行前工作区里就有 1 处改动' in asked.stderr
    assert '运行前就有的 1 处改动不进提交' in asked.stderr
    assert asked.git == 'declined'


def test_follow_tags_does_not_push_a_private_tag(tmp_path: Path):
    root = _repository(tmp_path)
    config = _global_config(tmp_path)
    config.write_text(config.read_text(encoding='utf-8') + '[push]\n\tfollowTags = true\n')
    _git(root, tmp_path, 'tag', '-a', 'v0-private', '-m', 'not for publishing')

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert run.git == 'pushed'
    assert _git(tmp_path / 'remote.git', tmp_path, 'tag', '--list') == ''


def test_the_summary_shows_where_the_push_really_goes(tmp_path: Path):
    root = _repository(tmp_path)
    other = tmp_path / 'other.git'
    _git(tmp_path, tmp_path, 'init', '-q', '--bare', str(other))
    _git(root, tmp_path, 'push', '-q', str(other), 'main')
    _git(root, tmp_path, 'config', 'remote.origin.pushurl', str(other))

    run = _run(tmp_path, _script(), root=root, stdin='1\n')

    assert f'上游 origin/main（推送到 {other}）' in run.stderr
    assert run.git == 'pushed'
    assert _git(other, tmp_path, 'rev-parse', 'main').strip() == _head(root, tmp_path)


def test_file_mode_false_does_not_turn_a_good_commit_into_a_failure(tmp_path: Path):
    root = _repository(tmp_path)
    _git(root, tmp_path, 'config', 'core.fileMode', 'false')
    calls = (_write('run.sh', 'echo hi\n'), _call('b', 'Bash', command='chmod +x run.sh'))

    run = _run(tmp_path, _script(*calls), root=root, git_mode='commit')

    assert (run.code, run.git) == (0, 'committed')


def test_a_closed_stdin_means_3(tmp_path: Path):
    root = _repository(tmp_path)
    head = _head(root, tmp_path)

    run = _run(tmp_path, _script(), root=root, stdin=cast(TextIO, None))

    assert (run.code, run.git) == (0, 'declined')
    assert _head(root, tmp_path) == head


# ---- 收尾之后交接文件没写成 ----


def test_a_handoff_directory_removed_during_the_question_still_prints_the_result(
    tmp_path: Path,
):
    root = _repository(tmp_path)

    def remove() -> None:
        shutil.rmtree(root / '.syndica' / 'handoffs')

    run = _run(tmp_path, _script(), root=root, stdin=_Answering('2\n', remove))

    assert run.code == 1
    assert (run.git, run.matter['exit_code']) == ('committed', '1')
    assert f'提交：`{_head(root, tmp_path)}`' in run.stdout
    assert '不要为此重跑' in run.stdout


def test_a_failed_final_rewrite_after_a_push_still_prints_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def full(*args: object, **kwargs: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(host_module, 'rewrite_handoff', full)
    root = _repository(tmp_path)

    run = _run(tmp_path, _script(), root=root, git_mode='push')

    assert run.code == 1
    assert (run.git, run.matter['exit_code']) == ('pushed', '1')
    assert _remote_head(tmp_path) == _head(root, tmp_path)
    assert 'disk full' in run.stderr
    (path,) = (root / '.syndica' / 'handoffs').iterdir()
    assert 'git: pending' in path.read_text(encoding='utf-8')
