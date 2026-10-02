"""`run --handoff`（spec D13）：交接文件的判定、组装与落盘。

机器可读的只有开头的 front matter：键与值都是 ASCII、键序固定，一旦发布就是兼容承诺。正文的节标题与
措辞是展示层。来自模型、Agent 或文件系统的字符串进终端与文件之前都经 `terminal_safe`；整份文件在落盘
之前经脱敏器（D12），写在 `.syndica/handoffs/` 下、权限 0600。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from syndica.cli.dev_module import DevLogSnapshot
from syndica.cli.host_git import Change
from syndica.cli.terminal import one_line, terminal_safe
from syndica.kernel.redaction import Redactor, write_all

__all__ = [
    'SCHEMA',
    'HandoffFacts',
    'HandoffLocked',
    'HandoffWriteError',
    'RedactingWriter',
    'VerifyResult',
    'commit_title',
    'exit_code_of',
    'front_matter',
    'lock_workspace',
    'marker_verdict',
    'path_key',
    'protected_matcher',
    'quoted',
    'render',
    'reviewer_verdict',
    'rewrite_handoff',
    'status_of',
    'terminal_safe',
    'trace_directory',
    'write_handoff',
]

SCHEMA = 'syndica.handoff/1'
# D15：Agent 改忽略规则就能让本该被忽略的文件（run 中途新建的 `.env`）变成「新改动」。
BUILTIN_PROTECTED = ('**/.gitignore', '**/.gitattributes')
_OWN = '.syndica'
_HANDOFFS = 'handoffs'
_TRACES = 'traces'
# 公开契约里的标记行语法，逐行整行匹配（`\s` 按契约，含 `\r`、NBSP 与全角空格：只认空格与制表符
# 时，CRLF 结尾的 `verdict: block` 不算数，后面一行 `verdict: pass` 就成了唯一的标记）。
# 值按 ASCII 不分大小写：Unicode 的大小写折叠会让 `paſſ`（U+017F）也匹配上，front matter 就不再是
# ASCII、也不在取值表里。
_VERDICT = re.compile(r'\s*verdict\s*[:：]\s*(?ai:(pass|partial|block|unverified))\s*')
_COMMIT_TITLE = re.compile(r'\s*commit_title\s*[:：]\s*(\S.*?)\s*')


class HandoffLocked(Exception):
    """同一工作区已有一个 `--handoff` run 在跑。"""


class HandoffWriteError(Exception):
    """交接文件写不下去：`.syndica` 或 `handoffs` 是符号链接、不是目录，或解析到工作区外。三件套
    与内核的错误同形，CLI 按期望 / 实际 / 下一步打出来。"""

    def __init__(self, *, expected: str, received: str, next_action: str) -> None:
        self.expected = expected
        self.received = received
        self.next_action = next_action
        super().__init__(f'期望: {expected}\n实际: {received}\n下一步: {next_action}')


def _single(pattern: re.Pattern[str], text: str | None) -> str | None:
    """恰好一行整行匹配时的那个值。按 `splitlines` 切行：认的换行越多，藏在同一「行」里的第二个
    标记越少，只会让结论从一个值变成缺失，不会反过来。"""
    if text is None:
        return None
    found = [match.group(1) for line in text.splitlines() if (match := pattern.fullmatch(line))]
    return found[0] if len(found) == 1 else None


def marker_verdict(text: str | None) -> str | None:
    """Reviewer 文字里恰好一行 `verdict: <值>` 时的值（小写）；零行或多行都是 None。"""
    value = _single(_VERDICT, text)
    return None if value is None else value.lower()


def commit_title(text: str | None) -> str | None:
    """Orchestrator 最终回答里恰好一行 `commit_title: <标题>` 时的标题；零行或多行都是 None。"""
    return _single(_COMMIT_TITLE, text)


def _glob(pattern: str) -> str:
    """D15 的 glob：`**` 跨任意层目录，`*` 与 `?` 不跨 `/`，整串匹配。不用 `PurePath.match`：
    3.11 的它不递归 `**`。"""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith('**/', index):
            parts.append('(?:[^/]+/)*')
            index += 3
        elif pattern.startswith('**', index):
            parts.append('.*')
            index += 2
        elif pattern[index] == '*':
            parts.append('[^/]*')
            index += 1
        elif pattern[index] == '?':
            parts.append('[^/]')
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return ''.join(parts)


def protected_matcher(patterns: Iterable[str]) -> Callable[[str], bool]:
    """受保护路径的判定：内置的两条加 `workspace.protected`，对 POSIX 形式的相对路径整串匹配。
    不分大小写：macOS 的文件系统不分，git 照样把 `.GitIgnore` 当忽略规则读，ruff 照样读
    `RUFF.TOML`。"""
    alternatives = (f'(?:{_glob(p.casefold())})' for p in (*BUILTIN_PROTECTED, *patterns))
    compiled = re.compile('|'.join(alternatives))
    return lambda path: compiled.fullmatch(path.casefold()) is not None


def reviewer_verdict(log: DevLogSnapshot, snapshot_a: tuple[Change, ...] | None) -> str:
    """D14 的 Reviewer 结论：结束得最晚的、满足全部条件的那次 Reviewer 运行的标记行——结局是
    completed；开始与结束时的工作树指纹都等于快照 A；文字里恰有一行标记；这个 Agent 自己没有 Write /
    Edit、没启动过后台命令，而且它的**每一次**运行都取到了指纹、开始与结束时相同。最后一条挡住的是
    被唤醒两次的 Reviewer：第一次用 Bash 改了文件，第二次开始与结束都等于快照 A。找不到就是
    unverified：从来没派过 Reviewer、指纹缺失、Reviewer 之后又有人改都落在这里。"""
    if snapshot_a is None:
        return 'unverified'
    writers = {entry.agent_id for entry in log.tools if entry.tool in {'Write', 'Edit'}}
    background = {entry.agent_id for entry in log.tools if entry.handle is not None}
    dirty = {
        run.agent_id
        for run in log.runs
        if run.start is None
        or run.end is None
        or run.start.changes is None
        or run.start.changes != run.end.changes
    }
    qualified = [
        run
        for run in log.runs
        if run.ended is not None
        and run.stop_reason == 'completed'
        and run.agent_id not in writers | background | dirty
        and run.end is not None
        and run.end.changes == snapshot_a
    ]
    for run in sorted(qualified, key=lambda run: cast(int, run.ended), reverse=True):
        verdict = marker_verdict(run.final_text)
        if verdict is not None:
            return verdict
    return 'unverified'


def path_key(path: str) -> str:
    """比较两个路径是不是同一个文件用的键：Unicode 规范化成 NFC 再折叠大小写。macOS 的文件系统
    两样都不分，模型写 `SRC/café.py`（NFD）改到的就是 git 报出来的 `src/café.py`。"""
    return unicodedata.normalize('NFC', path).casefold()


def status_of(*, run_failed: bool, read_only: bool, host_verify: str, verdict: str) -> str:
    """按顺序取第一个成立的：run 失败；只读运行；复核失败；复核通过且 Reviewer 为 pass；其余。"""
    if run_failed:
        return 'run_failed'
    if read_only:
        return 'read_only'
    if host_verify == 'failed':
        return 'verification_failed'
    if host_verify == 'passed' and verdict == 'pass':
        return 'verified'
    return 'unverified'


def exit_code_of(status: str, git: str) -> int:
    """run 失败、复核失败、提交或推送失败退出 1，其余退出 0。"""
    if status in {'run_failed', 'verification_failed'} or git in {'failed', 'push_failed'}:
        return 1
    return 0


def front_matter(
    *,
    status: str,
    host_verify: str,
    verdict: str,
    git: str,
    protected_changed: Sequence[str],
    exit_code: int | None,
) -> str:
    changed = json.dumps(list(protected_changed), ensure_ascii=True)
    return (
        '---\n'
        f'schema: {SCHEMA}\n'
        f'status: {status}\n'
        f'host_verify: {host_verify}\n'
        f'reviewer_verdict: {verdict}\n'
        f'git: {git}\n'
        f'protected_changed: {changed}\n'
        f'exit_code: {"null" if exit_code is None else exit_code}\n'
        '---\n'
    )


@dataclass(frozen=True)
class VerifyResult:
    """一条 verify 命令的结果：命令、是否通过、检查摘要。"""

    command: str
    passed: bool
    summary: str


@dataclass(frozen=True)
class HandoffFacts:
    """组装正文用到的事实，都由宿主得出；只有 `answer` 与 Reviewer 的标记来自模型。
    `verify_error` 是复核没法下结论的原因（复核命令崩了、复核前后的快照取不到）。`trace` 是这次
    run 的 trace 目录（相对工作区根，没记录时 None），`trace_complete` 为假时它不完整。"""

    prompt: str
    answer: str | None
    failure: str | None
    changes: tuple[Change, ...] | None
    changes_error: str | None
    before_error: str | None
    touched_again: tuple[str, ...]
    written: frozenset[str]
    checks: tuple[str, ...]
    verify: tuple[VerifyResult, ...]
    verify_error: str | None
    changed_by_verify: tuple[str, ...]
    run_errors: tuple[str, ...]
    next_steps: tuple[str, ...]
    trace: str | None = None
    trace_complete: bool = True


def lock_workspace(root: Path) -> int:
    """对工作区根目录的目录描述符加排它锁，交回这个描述符；关掉它就放锁。不用 `.syndica/` 下的
    文件：Agent 删得掉它。"""
    descriptor = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise HandoffLocked(
            f'期望 {root} 上没有别的 `run --handoff` 在跑，实际它被另一个 run 锁着。等那个 run 结束'
            '（它退出时会把交接文件路径打到 stderr），或者到别的工作区里跑。'
        ) from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _checked_directory(root: Path, path: Path) -> bool:
    """这个目录要么不存在（交回 False），要么是工作区里的真目录（交回 True）；是符号链接或别的
    东西就拒绝：交接文件会写到它指向的地方去。"""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise HandoffWriteError(
            expected=f'{path} 是工作区里的真目录',
            received='它是符号链接',
            next_action='删掉它再跑：宿主不往链接指向的地方写交接文件与 trace',
        )
    if not stat.S_ISDIR(mode):
        raise HandoffWriteError(
            expected=f'{path} 是工作区里的真目录',
            received='它不是目录（是一个普通文件或别的类型的文件）',
            next_action='删掉或挪开它再跑：宿主的交接文件与 trace 写在这个目录里',
        )
    if not path.resolve().is_relative_to(root.resolve()):
        raise HandoffWriteError(
            expected=f'{path} 解析后仍在工作区 {root} 里',
            received=f'解析到了 {path.resolve()}',
            next_action='删掉它再跑',
        )
    return True


def _own_directory(root: Path, name: str) -> Path:
    """`.syndica/<name>`：两级都核对过（不是符号链接、是真目录、在工作区里），缺了就建（0700）。"""
    own = root / _OWN
    _ensure_directory(root, own)
    # 缺了就补，已有的不动：Agent 先建出 `.syndica/` 时第一次创建的那一刻已经过去了，而没有它交接
    # 文件与 trace 就出现在 `git status` 里。`O_EXCL` 在路径已存在时失败，是符号链接也一样。
    try:
        descriptor = os.open(own / '.gitignore', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        pass
    else:
        try:
            write_all(descriptor, b'*\n')
        finally:
            os.close(descriptor)
    directory = own / name
    _ensure_directory(root, directory)
    return directory


def _ensure_directory(root: Path, path: Path) -> None:
    """核对过的真目录，缺了就建（0700）。两个 run 同时开始时另一个可能抢先建好：那就照样核对它。"""
    if _checked_directory(root, path):
        return
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not _checked_directory(root, path):
            raise HandoffWriteError(
                expected=f'{path} 是工作区里的真目录',
                received='它刚被别的进程建出来又不见了',
                next_action='确认没有别的进程在删 .syndica，然后重跑',
            ) from None


def _handoff_directory(root: Path) -> Path:
    return _own_directory(root, _HANDOFFS)


def trace_directory(root: Path) -> Path:
    """`.syndica/traces`：trace 的根目录，与交接文件同样核对，`.gitignore` 同样补。"""
    return _own_directory(root, _TRACES)


def write_handoff(root: Path, name: str, text: str, redactor: Redactor) -> Path:
    """第一次落盘：0600；`O_CREAT | O_EXCL` 在路径已存在时失败，是符号链接也一样，不会顺着它写到
    别处。编码不会失败（落单的代理字符转义成 `\\ud83d` 这样的文字）；写失败时删掉半截文件。"""
    path = _handoff_directory(root) / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            write_all(descriptor, redactor.redact(text).encode('utf-8', 'backslashreplace'))
        finally:
            os.close(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def rewrite_handoff(root: Path, path: Path, text: str, redactor: Redactor) -> None:
    """用最终内容替换同一个文件：先重新核对两级目录（第一次落盘与这次之间，宿主在等人选择），再写
    临时文件、原子改名，仍是 0600；中途失败时原文件保持原样，临时文件删掉（留着它，下一次改写会在
    `O_EXCL` 上失败）。"""
    if not (_checked_directory(root, root / _OWN) and _checked_directory(root, path.parent)):
        raise HandoffWriteError(
            expected=f'{path.parent} 仍在',
            received='第一次落盘之后它被删掉了',
            next_action='交接内容照样打印在 stdout 上；要留档就把它存下来',
        )
    temporary = path.with_name(f'.{path.name}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            write_all(descriptor, redactor.redact(text).encode('utf-8', 'backslashreplace'))
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class RedactingWriter:
    """`--handoff` 下 stdout 与 stderr 的写入器：按行攒齐再脱敏写出，跨两次 `write` 的 key 也
    认得出。`flush` 把不完整的那一行也写出去：问人的提示不以换行结尾，不写出去人就看不见问题。"""

    def __init__(self, stream: TextIO, redactor: Redactor) -> None:
        self._stream = stream
        self._redactor = redactor
        self._pending = ''

    def write(self, text: str) -> int:
        self._pending += text
        complete, newline, rest = self._pending.rpartition('\n')
        if newline:
            self._stream.write(self._redactor.redact(complete + newline))
            self._pending = rest
        return len(text)

    def flush(self) -> None:
        if self._pending:
            self._stream.write(self._redactor.redact(self._pending))
            self._pending = ''
        self._stream.flush()


def _listed(items: Collection[str], limit: int = 30) -> list[str]:
    shown = [f'- {one_line(item)}' for item in list(items)[:limit]]
    if len(items) > limit:
        shown.append(f'- ……共 {len(items)} 项')
    return shown


def quoted(text: str) -> list[str]:
    """来自模型的多行文字按引用块写：每一行都以 `> ` 开头，它伪造不出交接文件自己的节。"""
    return [f'> {line}' for line in terminal_safe(text).split('\n')]


def render(matter: str, facts: HandoffFacts, *, git_section: str) -> str:
    """整份交接文件：front matter 加正文。正文里模型与文件系统给的字符串都已转义。"""
    lines = [matter, '# 交接', '', '## 目标', '', *quoted(facts.prompt), '', '## 改动', '']
    if facts.changes is None:
        lines.append(f'列不出改动：{one_line(facts.changes_error or "未知原因")}')
    elif not facts.changes:
        lines.append('没有改动。')
    else:
        for change in facts.changes[:30]:
            produced = path_key(change.path) in facts.written
            note = '' if produced else '（不是 Write / Edit 产生的）'
            lines.append(f'- `{change.status}` {one_line(change.path)}{note}')
        if len(facts.changes) > 30:
            lines.append(f'- ……共 {len(facts.changes)} 处')
    if facts.before_error is not None:
        lines += [
            '',
            '运行前的快照没取到，确认不了这次有没有改到人在运行前就改过的文件：'
            f'{one_line(facts.before_error)}',
        ]
    if facts.touched_again:
        lines += [
            '',
            '运行前已经改过、这次又被改的文件（提交前请人核对）：',
            *_listed(facts.touched_again),
        ]
    lines += ['', '## Agent 跑过的检查', '']
    lines += [f'- {one_line(check)}' for check in facts.checks] or ['没有。']
    lines += ['', '## 宿主复核', '']
    if not facts.verify and facts.verify_error is None:
        lines.append('没有复核：没配 `workspace.verify`，或 run 没有正常结束。')
    for result in facts.verify:
        verdict = '通过' if result.passed else '失败'
        lines.append(f'- {verdict}：`{one_line(result.command)}` —— {result.summary}')
    if facts.verify_error is not None:
        lines += ['', f'复核没法下结论，按失败算：{one_line(facts.verify_error)}']
    if facts.changed_by_verify:
        lines += ['', '复核命令改了这些文件，复核按失败算：', *_listed(facts.changed_by_verify)]
    if facts.run_errors:
        lines += ['', '## 运行中的错误', '']
        for error in facts.run_errors:
            lines += [*quoted(error), '']
    lines += ['', '## Agent 的结论', '', '（模型给出的文字，不是宿主的结论。）', '']
    if facts.failure is not None:
        lines += ['run 没有正常结束：', *quoted(facts.failure), '']
    lines += quoted(facts.answer) if facts.answer else ['没有回答。']
    lines += ['', '## 下一步', '', *[f'- {step}' for step in facts.next_steps]]
    if facts.trace is not None:
        lines += ['', '## trace', '', f'`{one_line(facts.trace)}`']
        if not facts.trace_complete:
            lines += ['', '不完整：有记录没写进去（原因在 run 的 stderr 里），不要拿它回放或导出。']
    lines += ['', '## git 收尾', '', git_section, '']
    return '\n'.join(lines)
