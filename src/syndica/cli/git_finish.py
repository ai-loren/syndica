"""git 收尾（spec D14）：人选择，宿主执行。

只在 `run --handoff` 且不是 `--no-write` 时进行。快照、状态、hash、推送在宿主 git 环境里跑
（`HostGit`）；提交在 Agent 的环境里跑（`hook_environment`）：钩子与配置里的程序执行的往往是
工作区里 Agent 能改的代码，它们只能拿到 Agent 本来就有的东西。提交的路径、推送的 remote、分支与
提交本身都来自运行前的快照与这次提交，不来自 Agent。提交消息从 stdin 给、经脱敏；宿主不加任何
trailer。
"""

from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import termios
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from syndica.cli.handoff import quoted
from syndica.cli.host_git import LOCAL_TIMEOUT, Change, GitUnavailable, HostGit, host_environment
from syndica.cli.redaction import Redactor
from syndica.cli.terminal import one_line

__all__ = [
    'PUSH_TIMEOUT',
    'Finish',
    'Integrity',
    'Offer',
    'Outcome',
    'ahead_of_upstream',
    'ask',
    'commit_message',
    'execute',
    'finish',
    'hook_environment',
    'ignored_paths',
    'integrity',
    'offer',
    'summary',
    'verification_gaps',
]

PUSH_TIMEOUT = 120.0
_TITLE_LIMIT = 72
# 超时之后先 SIGTERM 整组（git 借此删掉自己的锁文件），等这么久还没退出再 SIGKILL。
_STOP_GRACE = 5.0
# 进行中的操作在 git 目录里留下的标记。
_IN_PROGRESS = (
    ('MERGE_HEAD', 'merge'),
    ('rebase-merge', 'rebase'),
    ('rebase-apply', 'rebase 或 am'),
    ('CHERRY_PICK_HEAD', 'cherry-pick'),
    ('REVERT_HEAD', 'revert'),
    ('BISECT_LOG', 'bisect'),
)
# 推送时会在本地运行的钩子。推送要用宿主的环境（ssh agent、凭证），钩子会拿到它们，而钩子执行的往往
# 是工作区里 Agent 能改的代码：有这样的钩子就不提供推送，推送时也关掉本地钩子。
_PUSH_HOOKS = frozenset({'pre-push', 'reference-transaction'})
# 本机可能经 trace2 事件目标挂着一个改写提交消息的进程（会追加署名），而提交消息要与生成的逐字
# 一致：提交与它触发的钩子都不发 trace2 事件。全局配置里的事件目标只有这个环境变量盖得过，
# 命令行 `-c` 盖不过。
_NO_TRACE2_EVENTS = {'GIT_TRACE2_EVENT': '0'}
_HOST_GIT_CONFIG = ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')
_FIELDS = (
    ('toplevel', '顶层目录'),
    ('head', 'HEAD'),
    ('branch', '分支'),
    ('upstream', '上游'),
    ('upstream_head', '上游分支在本地的位置'),
    ('push_urls', '推送地址'),
    ('config', 'git 配置（`git config --list` 的结果）'),
    ('hooks', '钩子'),
    ('in_progress', '进行中的操作'),
)
_NO_SNAPSHOT = '取不到仓库的完整性快照（不在 git 仓库里，或 git 用不了）'
_MANUAL = '要提交就自己核对改动，再 `git add` / `git commit`；宿主没有动索引与提交历史。'
_TAMPERED = (
    '仓库的配置或钩子在 run 期间变了：先用编辑器或 `cat` 看 `.git/config`、钩子目录与 run 期间'
    '改过的配置文件，不要先跑 git 命令——被改过的 `core.fsmonitor` 在 `git status` 时就会执行。'
    '确认没有问题再自己提交。'
)


def hook_environment(host: Mapping[str, str], agent: Mapping[str, str]) -> dict[str, str]:
    """提交（以及它触发的钩子、`gpg.program` 这类配置里的程序）在这个环境里跑：它们执行的往往是
    工作区里 Agent 能改的代码，只能拿到 Agent 本来就有的东西——没有 ssh agent、没有像密钥的变量。
    git 配置仍用宿主的（身份、钩子目录、签名）：`GIT_CONFIG_GLOBAL` / `GIT_CONFIG_NOSYSTEM` 取宿主
    的值，宿主没设就不设。"""
    values = {name: value for name, value in agent.items() if name not in _HOST_GIT_CONFIG}
    values.update({name: host[name] for name in _HOST_GIT_CONFIG if name in host})
    return {**host_environment(values), **_NO_TRACE2_EVENTS}


def _attempt(git: HostGit, *args: str) -> str | None:
    """跑一条可以失败的 git（查询不存在的引用、没有上游）：失败时交回 None。"""
    try:
        return git.run(*args).decode('utf-8', 'surrogateescape').strip()
    except GitUnavailable:
        return None


@dataclass(frozen=True)
class Integrity:
    """run 开始前的完整性快照：问人之前、提交之前、推送之前各比一次，不一致就不往下走——Agent 改了
    配置（fsmonitor、remote、`pushInsteadOf`、`gpg.program`，含 include 进来的文件）、钩子或上游，
    宿主的提交与推送就会替它执行命令、推到别处、带走人没打算推的提交。`config` 是
    `git config --list --show-origin --show-scope` 输出的 sha256：各层配置与 include 进来的文件
    都在里面，值本身不留。"""

    toplevel: str
    head: str | None
    branch: str | None
    upstream: tuple[str, str] | None
    upstream_head: str | None
    push_urls: tuple[str, ...]
    config: str
    hooks: tuple[tuple[str, str], ...]
    in_progress: tuple[str, ...]

    @property
    def push_hooks(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.hooks if name in _PUSH_HOOKS)


def _git_path(git: HostGit, name: str) -> Path:
    raw = git.run('rev-parse', '--git-path', name).decode('utf-8', 'surrogateescape').strip()
    path = Path(raw)
    return path if path.is_absolute() else git.root / path


def _hashes(git: HostGit, paths: Sequence[Path]) -> list[str]:
    if not paths:
        return []
    return git.run('hash-object', '--no-filters', '--', *map(str, paths)).decode().split()


def integrity(git: HostGit) -> Integrity:
    """钩子目录按宿主环境解析（`core.hooksPath` 在全局配置里也算）；钩子文件的 hash 用不写对象库的
    `hash-object`。"""
    toplevel = git.run('rev-parse', '--show-toplevel').decode('utf-8', 'surrogateescape').strip()
    branch = _attempt(git, 'symbolic-ref', '-q', '--short', 'HEAD')
    upstream = None
    upstream_head = None
    push_urls: tuple[str, ...] = ()
    if branch:
        remote = _attempt(git, 'config', '--get', f'branch.{branch}.remote')
        merge = _attempt(git, 'config', '--get', f'branch.{branch}.merge')
        if remote and merge:
            upstream = (remote, merge)
            upstream_head = _attempt(git, 'rev-parse', '-q', '--verify', '@{u}')
            urls = _attempt(git, 'remote', 'get-url', '--push', '--all', remote)
            push_urls = tuple(urls.splitlines()) if urls else ()
    listing = git.run('config', '--list', '-z', '--show-origin', '--show-scope')
    hooks_dir = _git_path(git, 'hooks')
    hook_files = sorted(p for p in hooks_dir.iterdir() if p.is_file()) if hooks_dir.is_dir() else []
    # 可执行位也算：git 只跑可执行的钩子，run 期间给一个原本不跑的钩子 `chmod +x` 就让它跑起来。
    hook_marks = [
        f'{"x" if os.access(p, os.X_OK) else "-"} {digest}'
        for p, digest in zip(hook_files, _hashes(git, hook_files), strict=True)
    ]
    return Integrity(
        toplevel=toplevel,
        head=_attempt(git, 'rev-parse', '--verify', '-q', 'HEAD'),
        branch=branch or None,
        upstream=upstream,
        upstream_head=upstream_head,
        push_urls=push_urls,
        config=hashlib.sha256(listing).hexdigest(),
        hooks=tuple(zip((p.name for p in hook_files), hook_marks, strict=True)),
        in_progress=tuple(label for name, label in _IN_PROGRESS if _git_path(git, name).exists()),
    )


def _changed(before: Integrity, now: Integrity) -> list[str]:
    """两份快照里不同的项，钩子具体到文件名：人要知道该去看哪里。"""
    changed: list[str] = []
    for field, label in _FIELDS:
        if getattr(before, field) == getattr(now, field):
            continue
        if field == 'hooks':
            names = sorted({name for name, _ in set(before.hooks) ^ set(now.hooks)})
            label = f'钩子 {"、".join(map(one_line, names))}'
        changed.append(label)
    return changed


def ignored_paths(git: HostGit) -> frozenset[str]:
    """运行前被忽略的文件与目录（目录以 `/` 结尾），相对工作区：它们出现在改动里（Agent 改了忽略
    规则），就不自动提交。"""
    prefix = git.prefix()
    raw = git.run(
        'status', '--porcelain=v1', '-z', '--ignored=matching', '--untracked-files=all', '--', '.'
    )
    found: set[str] = set()
    for field in raw.decode('utf-8', 'surrogateescape').split('\0'):
        if field.startswith('!! '):
            path = field[3:]
            found.add(path[len(prefix) :] if path.startswith(prefix) else path)
    return frozenset(found)


def _was_ignored(path: str, ignored: Collection[str]) -> bool:
    return path in ignored or any(
        entry.endswith('/') and path.startswith(entry) for entry in ignored
    )


def ahead_of_upstream(git: HostGit) -> int | None:
    """本地领先上游几个提交；没有上游或算不出来是 None。"""
    counted = _attempt(git, 'rev-list', '--count', '@{u}..HEAD')
    return int(counted) if counted is not None and counted.isdigit() else None


@dataclass(frozen=True)
class Offer:
    """能提供的选项。`reasons` 是提交的前置条件哪几条不满足（空就是能提交）；`push_reason` 是为什么
    没有推送选项；`protected` 是改到的受保护路径——它只挡 `--git-mode`，交互询问时醒目列出；
    `tampered` 表示仓库的配置或钩子在 run 期间变了，人自己动手之前要先查它们。"""

    reasons: tuple[str, ...]
    push_reason: str | None
    protected: tuple[str, ...]
    tampered: bool = False

    @property
    def commit(self) -> bool:
        return not self.reasons

    @property
    def push(self) -> bool:
        return self.commit and self.push_reason is None


def verification_gaps(*, status: str, host_verify: str, verdict: str) -> list[str]:
    """交接状态不是 verified 的具体原因，空就是 verified：人要知道该补的是哪一条。"""
    if status == 'verified':
        return []
    gaps: list[str] = []
    if status == 'run_failed':
        gaps.append('run 没有正常结束')
    elif host_verify == 'not_configured':
        gaps.append('没有配置 workspace.verify：宿主没有复核')
    elif host_verify != 'passed':
        gaps.append('宿主复核没有全过')
    if status != 'run_failed' and verdict != 'pass':
        gaps.append(f'Reviewer 结论是 {verdict}，不是 pass')
    return gaps or [f'交接状态是 {status}，不是 verified']


def offer(
    *,
    root: Path,
    before: Integrity | None,
    now: Integrity | None,
    verification: Sequence[str],
    run_changes: Collection[Change],
    touched_again: Collection[str],
    before_changes: Collection[Change] | None,
    ignored_before: Collection[str] | None,
    protected: Collection[str],
    ahead: int | None,
) -> Offer:
    """D14 的前置条件，逐条检查、全部列出：人要知道的是哪几条挡住了，不只是第一条。"""
    if before is None or now is None:
        return Offer((_NO_SNAPSHOT,), None, tuple(sorted(protected)))
    reasons: list[str] = []
    if os.path.realpath(before.toplevel) != os.path.realpath(root):
        reasons.append(f'工作区根不是 git 顶层目录（顶层是 {one_line(before.toplevel)}）')
    changed = _changed(before, now)
    if changed:
        reasons.append(f'仓库的完整性快照与 run 开始前不一致（{"、".join(changed)} 变了）')
    if before.branch is None:
        reasons.append('不在分支上（游离 HEAD）')
    ongoing = {*before.in_progress, *now.in_progress}
    in_progress = [label for _, label in _IN_PROGRESS if label in ongoing]
    if in_progress:
        reasons.append(f'有进行中的 {"、".join(in_progress)}')
    reasons.extend(verification)
    if not run_changes:
        reasons.append('这次 run 没有改动')
    if before_changes is None:
        reasons.append('运行前的快照没取到，确认不了有没有改到人在运行前改过的文件')
    if touched_again:
        reasons.append('有运行前就改过、这次又被改的文件')
    if ignored_before is None:
        reasons.append('列不出运行前被忽略的文件，确认不了改动里有没有它们')
    else:
        unignored = sorted(c.path for c in run_changes if _was_ignored(c.path, ignored_before))
        if unignored:
            reasons.append(f'运行前被忽略的文件出现在改动里：{", ".join(map(one_line, unignored))}')
    push_reason = None
    if before.upstream is None:
        push_reason = '当前分支没有上游'
    elif before.push_hooks:
        push_reason = (
            f'仓库有推送时会在本地运行的钩子（{"、".join(before.push_hooks)}）：推送要用宿主的 ssh '
            'agent 与凭证，钩子会拿到它们，而钩子运行的代码 Agent 可能改过'
        )
    elif before_changes:
        push_reason = (
            f'运行前工作区里就有 {len(before_changes)} 处改动：复核时它们在场、提交里没有它们，'
            '这个提交没有被单独复核过'
        )
    elif ahead is None:
        push_reason = '算不出本地领先上游几个提交'
    elif ahead:
        push_reason = f'本地已经领先上游 {ahead} 个提交：推送会顺带推走以前没推的提交'
    return Offer(tuple(reasons), push_reason, tuple(sorted(protected)), tampered=bool(changed))


def summary(
    *,
    before: Integrity,
    changes: Sequence[Change],
    written: Callable[[str], bool],
    host_verify: str,
    verdict: str,
    protected: Sequence[str],
    pre_existing: int,
) -> str:
    """提问前最后一段输出：分支与上游（推送真正去的地址）、改动（最多 30 条再给总数，标出不是
    Write / Edit 产生的）、受保护路径、运行前就有的改动、提交时会跑的钩子、复核结果与 Reviewer
    结论。字符串都来自宿主或经过转义。"""
    upstream = '没有上游'
    if before.upstream is not None:
        remote, merge = before.upstream
        urls = (
            f'（推送到 {"、".join(map(one_line, before.push_urls))}）' if before.push_urls else ''
        )
        upstream = f'上游 {one_line(remote)}/{one_line(merge.removeprefix("refs/heads/"))}{urls}'
    lines = [
        f'git 收尾：分支 {one_line(before.branch or "?")} → {upstream}',
        f'改动（{len(changes)} 处）：',
    ]
    for change in changes[:30]:
        note = '' if written(change.path) else '（不是 Write / Edit 产生的）'
        lines.append(f'- `{change.status}` {one_line(change.path)}{note}')
    if len(changes) > 30:
        lines.append(f'- ……共 {len(changes)} 处')
    if protected:
        lines.append('！！受保护路径有改动（它们决定「检查通过」是什么意思，请逐个核对）：')
        lines += [f'- {one_line(path)}' for path in protected]
    if pre_existing:
        lines.append(f'运行前就有的 {pre_existing} 处改动不进提交（复核时它们在工作区里）。')
    hooks = [name for name, _ in before.hooks if not name.endswith('.sample')]
    if hooks:
        lines.append(
            f'提交时会运行钩子（在 Agent 的环境里，拿不到 ssh agent 与密钥）：'
            f'{"、".join(map(one_line, hooks))}'
        )
    lines.append(f'宿主复核：{host_verify}；Reviewer 结论（模型给出）：{verdict}')
    return '\n'.join(lines)


def _drain(stream: TextIO) -> None:
    """stdin 是终端时丢掉提问之前已经缓冲的输入：误敲的按键不能当成答复。"""
    try:
        descriptor = stream.fileno()
        if os.isatty(descriptor):
            termios.tcflush(descriptor, termios.TCIFLUSH)
    except (AttributeError, OSError, ValueError, termios.error):
        return


def ask(
    stdin: TextIO | None,
    stderr: TextIO,
    text: str,
    choices: Offer,
    *,
    drain: Callable[[TextIO], None] = _drain,
) -> str:
    """在 stderr 打摘要与可用的选项，读一行答复：交回 '1'（提交并推送）、'2'（只提交）或 '3'（我来
    处理）。EOF、空行、没提供的选项、乱输入与读不了的 stdin（关掉了、不存在）都是 '3'：
    `</dev/null` 的 run 永远不会自动提交。"""
    options = ['3']
    lines = [text, '']
    if choices.push:
        lines.append('  1. 提交并推送')
        options.append('1')
    if choices.commit:
        lines.append('  2. 只提交')
        options.append('2')
        if choices.push_reason is not None:
            lines.append(f'  （没有推送选项：{choices.push_reason}）')
    lines.append('  3. 我来处理')
    stderr.write('\n'.join(lines) + f'\n选哪个 [{"/".join(sorted(options))}]？> ')
    stderr.flush()
    if stdin is None:
        return '3'
    drain(stdin)
    try:
        answer = stdin.readline().strip()
    except (OSError, ValueError):
        return '3'
    return answer if answer in options else '3'


@dataclass(frozen=True)
class Outcome:
    """收尾的结果：`state` 是 front matter 的 `git` 取值之一；`commit` 是已经在本地的那个提交（失败
    时也填：人要知道有没有提交）；`detail` 是给人看的一段说明（为什么没提交、推送为什么失败）。"""

    state: str
    commit: str | None = None
    message: str | None = None
    detail: str | None = None


def _commit_title(title: str | None, prompt: str) -> str:
    first = title or next((line for line in prompt.splitlines() if line.strip()), 'syndica run')
    text = one_line(first.strip())
    return text if len(text) <= _TITLE_LIMIT else text[: _TITLE_LIMIT - 1] + '…'


def commit_message(title: str | None, prompt: str, verify: Sequence[tuple[str, str]]) -> str:
    """标题取 Orchestrator 的 `commit_title:`（缺了用目标的第一行），转义、截到 72 个字符；正文列出
    宿主复核。调用方先脱敏再交进来：截断可能切在一个密钥中间，剩下的那截不一定还认得出来。"""
    body = '\n'.join(f'- 通过：{one_line(command)} —— {summary_}' for command, summary_ in verify)
    return f'{_commit_title(title, prompt)}\n\n宿主复核：\n{body}\n'


def _added_lines(git: HostGit, change: Change) -> list[tuple[int, str]]:
    """这次提交会加进去的行：新文件是全部，改动的文件是 diff 里每个块之后的 `+` 行。新的符号链接
    提交的是链接文字，只看它：跟着链接读会扫到工作区外的文件，FIFO 会让 `read` 永远等下去。
    `--text` 不可少：`-diff` / `binary` 属性会让 diff 只说一句「二进制文件不同」。"""
    if change.blob is None:
        return []
    if change.status == '??':
        full = git.root / change.path
        mode = os.lstat(full).st_mode
        if stat.S_ISLNK(mode):
            data = os.readlink(full).encode('utf-8', 'surrogateescape')
        elif stat.S_ISREG(mode):
            data = full.read_bytes()
        else:
            return []
        return list(enumerate(data.decode('utf-8', 'replace').split('\n'), start=1))
    raw = git.run(
        'diff',
        '--no-color',
        '--no-ext-diff',
        '--no-textconv',
        '--text',
        '-U0',
        'HEAD',
        '--',
        change.path,
    )
    found: list[tuple[int, str]] = []
    number: int | None = None
    for line in raw.decode('utf-8', 'replace').split('\n'):
        if line.startswith('@@'):
            target = line.split('+', 1)[1].split(' ', 1)[0]
            number = int(target.split(',', 1)[0])
        elif number is not None and line.startswith('+'):
            found.append((number, line[1:]))
            number += 1
    return found


def _secrets(git: HostGit, changes: Sequence[Change], redactor: Redactor) -> list[str]:
    """用提交闸（逐字值与格式规则，不做片段匹配）扫将要提交的新增行与文件名：报文件:行号与规则
    ID，不报内容；文件名本身命中时报脱敏后的名字。"""
    findings: list[str] = []
    for change in changes:
        for finding in redactor.scan(change.path):
            findings.append(f'文件名 {redactor.redact(one_line(change.path))}（{finding.rule_id}）')
        lines = _added_lines(git, change)
        for finding in redactor.scan('\n'.join(text for _, text in lines)):
            number = lines[finding.line - 1][0]
            findings.append(f'{one_line(change.path)}:{number}（{finding.rule_id}）')
    return findings


def _stop(process: subprocess.Popen[bytes]) -> None:
    """超时之后停下整组：先 SIGTERM（git 借此删掉自己的锁文件），等不到再 SIGKILL。钩子起的子进程
    与 git 同组，不能比它活得久。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=_STOP_GRACE)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def _hooked(
    git: HostGit,
    env: Mapping[str, str],
    redactor: Redactor,
    args: Sequence[str],
    *,
    message: str = '',
) -> subprocess.CompletedProcess[bytes]:
    """跑一条会触发钩子的 git（add、commit、rm --cached）：在 `env` 里、在自己的进程组里跑；消息从
    stdin 给（没有消息时 stdin 是空的）。超时抛 `TimeoutExpired`，整组已经停下。"""
    process = subprocess.Popen(
        ['git', '-c', 'core.fsmonitor=false', '--attr-source=HEAD', *args],
        cwd=git.root,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(
            input=redactor.redact(message).encode('utf-8'), timeout=LOCAL_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        _stop(process)
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _committed(git: HostGit, paths: Sequence[str]) -> tuple[set[str], dict[str, tuple[str, str]]]:
    """HEAD 这个提交改到的路径集合，与这些路径在 HEAD 里的 (mode, blob)；删掉的路径不在后者里。"""
    touched = git.run('diff-tree', '-r', '-z', '--name-only', '--no-commit-id', '--root', 'HEAD')
    names = {name for name in touched.decode('utf-8', 'surrogateescape').split('\0') if name}
    listed = git.run('ls-tree', '-r', '-z', 'HEAD', '--', *paths)
    blobs: dict[str, tuple[str, str]] = {}
    for entry in listed.decode('utf-8', 'surrogateescape').split('\0'):
        if entry:
            meta, path = entry.split('\t', 1)
            mode, _, blob = meta.split(' ')
            blobs[path] = (mode, blob)
    return names, blobs


def _matches(change: Change, entry: tuple[str, str] | None, *, file_mode: bool) -> bool:
    """提交里的这一项是不是快照 A 里的那一份。`core.fileMode=false` 时 git 不记可执行位，只比
    内容与是不是符号链接。"""
    if change.blob is None:
        return entry is None
    if entry is None or entry[1] != change.blob:
        return False
    if file_mode:
        return entry[0] == change.mode
    return (entry[0] == '120000') == (change.mode == '120000')


def _push_failure(detail: str) -> str:
    lowered = detail.lower()
    # git 对「仓库不存在」也会补一句 "Could not read from remote repository"：先认它，
    # 否则会被当成认证失败。
    if 'does not appear to be a git repository' in lowered or 'repository not found' in lowered:
        return (
            f'期望推送到一个存在的远端仓库，实际找不到它：{detail}。确认 remote 的 URL'
            '（`git remote -v`）没写错、仓库还在，然后手动 `git push`。提交已经在本地。'
        )
    if 'permission denied' in lowered or 'authentication' in lowered or 'could not read' in lowered:
        return (
            f'期望推送通过远端认证，实际被拒：{detail}。确认 ssh agent 里有这个 remote 的密钥'
            '（或凭证可用），然后手动 `git push`。提交已经在本地。'
        )
    if 'remote rejected' in lowered or 'hook declined' in lowered:
        return (
            f'期望远端接受推送，实际远端拒绝了：{detail}。按远端给出的原因处理之后手动 '
            '`git push`。提交已经在本地。'
        )
    if 'non-fast-forward' in lowered or 'fetch first' in lowered or '[rejected]' in lowered:
        return (
            f'期望推送是快进，实际远端有本地没有的提交：{detail}。先 `git pull --rebase`，确认没有'
            '冲突之后手动 `git push`。提交已经在本地；宿主从不 `--force`。'
        )
    return f'期望推送成功，实际失败：{detail}。按这个原因处理之后手动 `git push`。提交已经在本地。'


def _commit_failed(
    git: HostGit,
    *,
    before: Integrity,
    env: Mapping[str, str],
    redactor: Redactor,
    new: Sequence[str],
    what: str,
) -> Outcome:
    """`git commit` 没有正常结束。HEAD 动了就是提交已经做了（post-commit 钩子超时、被停下）：报出
    它、不推送，不能让人照着「手动提交」再提交一次。没动就把为提交暂存的新文件撤回去。"""
    head = _attempt(git, 'rev-parse', '-q', '--verify', 'HEAD')
    if head is not None and head != before.head:
        return Outcome(
            'failed',
            commit=head,
            message=_attempt(git, 'log', '-1', '--format=%B', 'HEAD'),
            detail=f'git 提交没有正常结束（{what}），但提交已经在本地：没有推送。先看这个提交'
            f'（`git show --stat {head}`），没有问题再自己推送。',
        )
    notes = ['改动还在工作区里']
    if new:
        try:
            unstaged = (
                _hooked(
                    git, env, redactor, ['rm', '--cached', '-q', '--ignore-unmatch', '--', *new]
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            unstaged = False
        if unstaged:
            notes.append('为提交暂存的新文件已经撤回')
        else:
            notes.append(f'这些新文件还在暂存区里：{", ".join(map(one_line, new))}')
    try:
        lock = _git_path(git, 'index.lock')
    except GitUnavailable:
        lock = None
    if lock is not None and lock.exists():
        notes.append(f'仓库里留下了 {lock}：确认没有别的 git 进程在跑之后删掉它')
    return Outcome(
        'failed',
        detail=f'期望 git 提交成功，实际{what}。{"；".join(notes)}。按这个原因处理之后手动提交。',
    )


def execute(
    git: HostGit,
    *,
    changes: Sequence[Change],
    message: str,
    push: bool,
    before: Integrity,
    redactor: Redactor,
    hook_env: Mapping[str, str],
) -> Outcome:
    """提交这次 run 的改动（`changes` 来自快照 A），按需推送。提交之前再比一次完整性快照、再算一次
    hash、做 D12 扫描；提交在 `hook_env` 里跑；提交之后核对父提交、改到的路径集合与每个 blob；
    推送之前再比一次完整性快照，推的是这个提交本身。任何一步对不上都不再往下走。"""
    try:
        now = integrity(git)
        current = git.changes()
        findings = _secrets(git, changes, redactor)
        file_mode = _attempt(git, 'config', '--type=bool', '--get', 'core.fileMode') != 'false'
    except (GitUnavailable, OSError) as exc:
        return Outcome(
            'failed',
            detail=f'期望提交前的检查跑完，实际失败：{one_line(str(exc))}。没有提交，改动还在工作区'
            '里；按这个原因处理之后手动提交。',
        )
    changed = _changed(before, now)
    if changed:
        return Outcome(
            'failed',
            detail=f'问过之后、提交之前，仓库变了（{"、".join(changed)}），没有提交。{_TAMPERED}',
        )
    expected = {change.path: change for change in changes}
    current_by_path = {change.path: change for change in current if change.path in expected}
    if current_by_path != expected:
        moved = sorted(p for p in expected if current_by_path.get(p) != expected[p])
        return Outcome(
            'failed',
            detail=f'复核之后这些文件又变了，没有提交：{", ".join(map(one_line, moved))}。重跑 '
            '`syndica run --handoff`，让复核看到的就是要提交的。',
        )
    if findings:
        return Outcome(
            'failed',
            detail=f'将要提交的内容里有像密钥的东西，没有提交：{"、".join(findings)}。拿掉它们（写进'
            ' .gitignore 的文件或环境变量）之后再提交。',
        )
    paths = [change.path for change in changes]
    new = [change.path for change in changes if change.status == '??']
    try:
        if new:
            added = _hooked(git, hook_env, redactor, ['add', '--', *new])
            if added.returncode != 0:
                detail = added.stderr.decode('utf-8', 'replace').strip() or '（没有输出）'
                return _commit_failed(
                    git,
                    before=before,
                    env=hook_env,
                    redactor=redactor,
                    new=new,
                    what=f' `git add` 退出 {added.returncode}：{one_line(detail)}',
                )
        committed = _hooked(
            git,
            hook_env,
            redactor,
            ['commit', '--only', '--cleanup=verbatim', '-F', '-', '--', *paths],
            message=message,
        )
    except subprocess.TimeoutExpired:
        return _commit_failed(
            git,
            before=before,
            env=hook_env,
            redactor=redactor,
            new=new,
            what=f'在 {LOCAL_TIMEOUT:g} 秒内没有结束（多半是钩子卡住了），已经停下它',
        )
    except OSError as exc:
        return _commit_failed(
            git,
            before=before,
            env=hook_env,
            redactor=redactor,
            new=new,
            what=f'起不来：{one_line(str(exc))}',
        )
    if committed.returncode != 0:
        detail = committed.stderr.decode('utf-8', 'replace').strip() or '（没有输出）'
        return _commit_failed(
            git,
            before=before,
            env=hook_env,
            redactor=redactor,
            new=new,
            what=f'退出 {committed.returncode}：{one_line(detail)}',
        )
    head: str | None = None
    actual: str | None = None
    try:
        head = git.run('rev-parse', 'HEAD').decode().strip()
        actual = git.run('log', '-1', '--format=%B', 'HEAD').decode('utf-8', 'replace')
        parent = _attempt(git, 'rev-parse', '-q', '--verify', 'HEAD^')
        names, blobs = _committed(git, paths)
    except (GitUnavailable, OSError) as exc:
        return Outcome(
            'failed',
            commit=head,
            message=actual,
            detail=f'提交做了，但核对不了它改到了什么：{one_line(str(exc))}。没有推送；检查这个'
            '提交。',
        )
    if parent != before.head:
        return Outcome(
            'failed',
            commit=head,
            message=actual,
            detail='这个提交的父提交不是 run 开始时的 HEAD（提交前后有别的提交进来了），没有推送：'
            '检查 `git log` 之后再决定。',
        )
    wrong = sorted(
        path
        for path, change in expected.items()
        if not _matches(change, blobs.get(path), file_mode=file_mode)
    )
    if names != set(paths) or wrong:
        return Outcome(
            'failed',
            commit=head,
            message=actual,
            detail='提交改到的路径或内容与复核时的不一致（可能是提交钩子改了文件），没有推送：'
            f'{", ".join(map(one_line, sorted(names ^ set(paths)) + wrong))}。检查这个提交。',
        )
    if not push or before.upstream is None:
        return Outcome('committed', commit=head, message=actual)
    try:
        now = integrity(git)
    except (GitUnavailable, OSError) as exc:
        return Outcome(
            'push_failed',
            commit=head,
            message=actual,
            detail=f'推送前复查仓库失败：{one_line(str(exc))}。没有推送；提交已经在本地。',
        )
    changed = _changed(replace(before, head=head), now)
    if changed:
        return Outcome(
            'push_failed',
            commit=head,
            message=actual,
            detail=f'提交之后、推送之前，仓库变了（{"、".join(changed)}），没有推送；提交已经在本地。'
            f'{_TAMPERED}',
        )
    remote, merge = before.upstream
    try:
        pushed = git.attempt(
            '-c',
            f'core.hooksPath={os.devnull}',
            '-c',
            'push.followTags=false',
            'push',
            '--no-verify',
            '--recurse-submodules=no',
            remote,
            f'{head}:{merge}',
            timeout=PUSH_TIMEOUT,
        )
    except GitUnavailable as exc:
        return Outcome(
            'push_failed',
            commit=head,
            message=actual,
            detail=f'推送没能跑完：{one_line(str(exc))}。提交已经在本地；处理之后手动 `git push`。',
        )
    if pushed.returncode != 0:
        detail = pushed.stderr.decode('utf-8', 'replace').strip() or '（没有输出）'
        return Outcome(
            'push_failed', commit=head, message=actual, detail=_push_failure(one_line(detail))
        )
    return Outcome('pushed', commit=head, message=actual)


@dataclass(frozen=True)
class Finish:
    """git 收尾的结局：`state` 是 front matter 的 `git`；`section` 是交接文件的「git 收尾」一节；
    `refused` 表示 `--git-mode commit|push` 而对应的前置条件不满足、没有执行（退出 3）。"""

    state: str
    section: str
    refused: bool = False


def _advice(choices: Offer) -> str:
    return _TAMPERED if choices.tampered else _MANUAL


def _not_offered(choices: Offer) -> str:
    lines = ['宿主没有提供自动提交，因为：', *(f'- {reason}' for reason in choices.reasons)]
    return '\n'.join([*lines, '', _advice(choices)])


def _refused(mode: str, why: str, items: Sequence[str], advice: str) -> Finish:
    lines = [f'`--git-mode {mode}` 没有执行：{why}', *(f'- {item}' for item in items)]
    return Finish('not_offered', '\n'.join([*lines, '', advice]), refused=True)


def _protected_note(protected: Sequence[str]) -> list[str]:
    if not protected:
        return []
    return [
        '受保护路径有改动，提交前已经在询问里逐个列出：',
        *(f'- {one_line(path)}' for path in protected),
        '',
    ]


def _outcome_section(
    outcome: Outcome, *, chosen: str, push_reason: str | None, protected: Sequence[str]
) -> str:
    lines = [f'选择：{chosen}。', '', *_protected_note(protected)]
    if outcome.commit is not None:
        lines.append(f'提交：`{outcome.commit}`')
    if outcome.state == 'committed':
        lines.append(
            '只提交，没有推送' + (f'（没有推送选项：{push_reason}）' if push_reason else '') + '。'
        )
    elif outcome.state == 'pushed':
        lines.append('已推送。')
    elif outcome.detail is not None:
        lines.append(outcome.detail)
    if outcome.message is not None:
        lines += [
            '',
            '提交后的实际消息（仓库的钩子可能改过它）：',
            '',
            *quoted(outcome.message.rstrip('\n')),
        ]
    return '\n'.join(lines)


def finish(
    git: HostGit,
    *,
    mode: str | None,
    start: Integrity | None,
    ignored_before: Collection[str] | None,
    before_changes: Collection[Change] | None,
    status: str,
    host_verify: str,
    verdict: str,
    run_changes: Sequence[Change],
    touched_again: Collection[str],
    protected: Sequence[str],
    written: Callable[[str], bool],
    title: str | None,
    prompt: str,
    verify: Sequence[tuple[str, str]],
    stdin: TextIO | None,
    stderr: TextIO,
    redactor: Redactor,
    hook_env: Mapping[str, str],
) -> Finish:
    """D14 的整个收尾：查前置条件 → 按 `--git-mode` 或问人 → 执行。`mode` 是 None 时在 stderr 上问；
    `manual` 等同于选 `3`：前置条件不满足时与交互一样是 `not_offered` 并列出原因。"""
    try:
        now = integrity(git) if start is not None else None
    except (GitUnavailable, OSError):
        now = None
    ahead = ahead_of_upstream(git) if start is not None and start.upstream is not None else None
    choices = offer(
        root=git.root,
        before=start,
        now=now,
        verification=verification_gaps(status=status, host_verify=host_verify, verdict=verdict),
        run_changes=run_changes,
        touched_again=touched_again,
        before_changes=before_changes,
        ignored_before=ignored_before,
        protected=protected,
        ahead=ahead,
    )
    if start is None or not choices.commit:
        if mode in {'commit', 'push'}:
            return _refused(mode, '提交的前置条件不满足：', choices.reasons, _advice(choices))
        return Finish('not_offered', _not_offered(choices))
    if mode == 'manual':
        return Finish('declined', f'`--git-mode manual`：没有提交。{_MANUAL}')
    if mode is not None:
        if choices.protected:
            return _refused(
                mode,
                '受保护路径有改动（交互运行时宿主会列出它们、由人决定，--git-mode 不替人决定）：',
                [one_line(path) for path in choices.protected],
                _MANUAL,
            )
        if mode == 'push' and not choices.push:
            return _refused(
                mode, '没有推送选项：', [choices.push_reason or '推送的前置条件不满足'], _MANUAL
            )
        push = mode == 'push'
        chosen = f'`--git-mode {mode}`'
    else:
        text = summary(
            before=start,
            changes=run_changes,
            written=written,
            host_verify=host_verify,
            verdict=verdict,
            protected=choices.protected,
            pre_existing=len(before_changes or ()),
        )
        answer = ask(stdin, stderr, text, choices)
        if answer == '3':
            return Finish(
                'declined',
                '\n'.join(
                    [
                        *_protected_note(choices.protected),
                        f'人选了「我来处理」：没有提交。{_MANUAL}',
                    ]
                ),
            )
        push = answer == '1'
        chosen = '提交并推送' if push else '只提交'
    message = commit_message(
        redactor.redact(title) if title is not None else None, redactor.redact(prompt), verify
    )
    try:
        outcome = execute(
            git,
            changes=run_changes,
            message=message,
            push=push,
            before=start,
            redactor=redactor,
            hook_env=hook_env,
        )
    except Exception as exc:
        # 收尾不能因为一个意外的异常丢掉「提交可能已经做了」这件事：交接文件要写明它，
        # 而不是让人重跑。
        head = _attempt(git, 'rev-parse', '-q', '--verify', 'HEAD')
        made = head is not None and head != start.head
        outcome = Outcome(
            'failed',
            commit=head if made else None,
            detail=f'git 收尾中途出错：{one_line(f"{type(exc).__name__}: {exc}")}。'
            + (
                '提交已经在本地，没有推送：先检查它再决定。'
                if made
                else '没有提交；处理之后手动提交。'
            ),
        )
    return Finish(
        outcome.state,
        _outcome_section(
            outcome,
            chosen=chosen,
            push_reason=None if push else choices.push_reason,
            protected=choices.protected if mode is None else (),
        ),
    )
