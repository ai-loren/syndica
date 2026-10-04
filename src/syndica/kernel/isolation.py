"""工作区隔离（M5 D6）：子 Agent 在父工作区的一份拷贝里干活，结束时逐文件合并回去。

副本从父的工作文件拷，不从版本层物化：版本层存的是脱过敏的内容，从它物化会把占位符写进子 Agent
的文件、再随合并写回父。两种隔离的差别只在合并：worktree 另存一份基线，两边都改过的文本文件交给
`git merge-file` 三方合并；快照只记基线的版本号，两边都改过就是冲突。没有 git 时 worktree 退回快照。

`git merge-file` 只在工作区之外新建的空临时目录里、对三个临时文件跑：那里没有仓库，系统与全局配置
都关掉，`GIT_` 开头的环境变量一个不留——它只读那三个文件，不会执行工作区里 Agent 写下的任何东西。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from syndica.kernel.errors import ConfigError
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.versions import _require_real_directories  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.workspace import Portal, WriteConflict

__all__ = ['ISOLATIONS', 'IsolatedCopy', 'LocalIsolator', 'MergeConflict', 'MergeReport']

logger = logging.getLogger(__name__)

ISOLATIONS: Final = ('shared', 'worktree', 'snapshot')
# 任何一层叫这两个名字的目录都不拷：`.git` 是用户的历史，`.syndica` 是宿主的记录与别的副本。
_SKIPPED = frozenset({'.git', '.syndica'})
# 拷贝在事件循环里同步做（隔离必须在子 Agent 的第一个周期之前就位，而那之前不能有挂起点）。
_MAX_FILES = 5000
_MAX_BYTES = 200 * 1024 * 1024
_MERGE_TIMEOUT = 30.0
_SIDE_ATTEMPTS = 20

Written = Callable[[str, bytes | None, str, bool], None]


@dataclass(frozen=True)
class MergeConflict:
    path: str
    reason: str
    theirs: str | None = None
    removed: bool = False


@dataclass(frozen=True)
class MergeReport:
    """一次合并的结果。`merged` 是写回父工作区（或本来就一致）的文件；`conflicts` 里的文件父工作区
    那份没动，子 Agent 的那份另存在 `theirs`（删除没有可存的内容时为 None）。"""

    agent_id: str
    strategy: str
    merged: tuple[str, ...]
    conflicts: tuple[MergeConflict, ...]
    note: str = ''
    kept: str | None = None

    @property
    def unsaved(self) -> bool:
        """有冲突的文件子 Agent 那份没能另存：这时副本不能删。"""
        return any(conflict.theirs is None and not conflict.removed for conflict in self.conflicts)

    def summary(self) -> str:
        lines = (
            [
                f'隔离副本（{self.strategy}）合并了一部分；有的冲突文件没能另存，'
                f'副本留在 {self.kept}。'
            ]
            if self.kept
            else [f'隔离副本（{self.strategy}）已合并回共享工作区并删除。']
        )
        if self.note:
            lines.append(self.note)
        lines.append(
            f'合并回 {len(self.merged)} 个文件'
            + (f'：{", ".join(self.merged)}' if self.merged else '')
        )
        for conflict in self.conflicts:
            kept = f'，它的那份在 {conflict.theirs}' if conflict.theirs else ''
            lines.append(f'冲突：{conflict.path}（{conflict.reason}）{kept}')
        if self.conflicts:
            lines.append(
                '冲突的文件共享工作区里那份没动；读两边的内容合成一份写回，再删掉 .theirs 文件。'
            )
        return '\n'.join(lines)


class IsolatedCopy:
    """一个子 Agent 的隔离副本。`base` 是拷贝那一刻每个文件的版本。"""

    def __init__(
        self,
        *,
        agent_id: str,
        strategy: str,
        location: Path,
        base_dir: Path | None,
        base: dict[str, str],
        workspace: LocalWorkspace,
        note: str,
    ) -> None:
        self.agent_id = agent_id
        self.strategy = strategy
        self.location = location
        self.base_dir = base_dir
        self.base = base
        self.workspace = workspace
        self.note = note
        self.merged_from: dict[str, str] | None = None

    def fact(self) -> str:
        """注入子 Agent 历史的环境事实（不进 System Prompt：它随 Agent 而变）。"""
        text = (
            '（环境）你在一份隔离的工作副本里干活：它是共享工作区在你启动那一刻的拷贝，路径照旧'
            '相对工作区根，别人此后的改动你看不到。你的改动在这次工作结束后逐文件合并回共享工作区；'
            '别人同时改过的文件算冲突，你的那份会另存成旁边的 .theirs 文件。'
        )
        if self.strategy == 'snapshot':
            text += '两边都改过的文件不做三方合并。'
        return text

    def changed(self) -> list[str]:
        """相对基线改过（含新建、删除）的文件。"""
        return _differences(self.base, _versions_under(self.location))

    def changed_since_merge(self) -> list[str]:
        """合并取快照之后副本里又变了的文件；没合并过时是空的。"""
        if self.merged_from is None:
            return []
        return _differences(self.merged_from, _versions_under(self.location))

    async def discard(self) -> None:
        await self.workspace.close()
        shutil.rmtree(self.location, ignore_errors=True)
        if self.base_dir is not None:
            shutil.rmtree(self.base_dir, ignore_errors=True)


class LocalIsolator:
    """本地工作区的隔离。`git` 是 git 可执行文件的绝对路径，没有就是 None（内核不读 PATH，由宿主
    找好交进来）；`scratch` 是工作区之外的一个目录，三方合并的临时文件建在它下面——`git merge-file`
    会在工作目录里找仓库，那个目录必须是 Agent 碰不到的。`shell` 与 `env` 同 `LocalWorkspace`。"""

    def __init__(
        self,
        *,
        shell: str,
        env: Mapping[str, str],
        git: str | None = None,
        scratch: str | None = None,
    ) -> None:
        for name, value in (('git', git), ('scratch', scratch)):
            if value is not None and not os.path.isabs(value):
                raise ConfigError(
                    expected=f'{name} 是绝对路径，或 None',
                    received=f'{name}={value!r}',
                    next_action=(
                        '宿主用 shutil.which("git") 与 tempfile.gettempdir() 找好再交进来'
                    ),
                )
        if git is not None and scratch is None:
            raise ConfigError(
                expected='给了 git 时也给 scratch：三方合并的临时文件要建在工作区之外',
                received='scratch=None',
                next_action='传 scratch=tempfile.gettempdir()，或者不传 git（worktree 退回快照）',
            )
        self._shell = shell
        self._env = dict(env)
        self._git = git
        self._scratch = scratch

    def isolate(self, root: Path, agent_id: str, requested: str) -> IsolatedCopy:
        if requested not in ('worktree', 'snapshot'):
            raise ConfigError(
                expected='isolation 是 worktree 或 snapshot（shared 不建副本）',
                received=f'isolation={requested!r}',
                next_action='改成 worktree、snapshot 或 shared',
            )
        strategy, note = requested, ''
        if requested == 'worktree' and self._git is None:
            strategy = 'snapshot'
            note = '没有 git，worktree 退回快照：两边都改过的文件一律算冲突，不做三方合并。'
        files = _working_files(root)
        location = root / '.syndica' / f'{strategy}s' / agent_id
        base_dir = location.with_name(f'{agent_id}.base') if strategy == 'worktree' else None
        # 先在旁边建好再整个换进来：拷到一半失败时不留半份副本，同一个 id 再隔离一次时也不会
        # 叠在旧副本上（旧副本里多出来的文件会被当成子 Agent 新建的合并回去）。
        building = [
            (folder, folder.with_name(f'.{folder.name}.building'))
            for folder in (location, base_dir)
            if folder is not None
        ]
        base: dict[str, str] = {}
        ignore = location.parent / '.gitignore'
        # 任何一级是符号链接，副本就拷到了工作区之外，合并时又从那里读回来。
        _require_real_directories(root, (root / '.syndica', location.parent))
        try:
            location.parent.mkdir(parents=True, exist_ok=True)
            if not ignore.exists():
                # 副本在用户的工作区里：不让它们出现在 `git status` 里，也不被宿主的自动提交带走。
                ignore.write_text('*\n', encoding='ascii')
            for _, scratch in building:
                shutil.rmtree(scratch, ignore_errors=True)
                scratch.mkdir(parents=True)
            for relative in files:
                source = root / relative
                data = source.read_bytes()
                mode = source.stat().st_mode & 0o777
                base[relative] = _version(data)
                for _, scratch in building:
                    target = scratch / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    os.chmod(target, mode)
            for folder, scratch in building:
                shutil.rmtree(folder, ignore_errors=True)
                os.replace(scratch, folder)
        except BaseException:
            for _, scratch in building:
                shutil.rmtree(scratch, ignore_errors=True)
            raise
        workspace = LocalWorkspace(str(location), shell=self._shell, env=self._env)
        return IsolatedCopy(
            agent_id=agent_id,
            strategy=strategy,
            location=location,
            base_dir=base_dir,
            base=base,
            workspace=workspace,
            note=note,
        )

    async def merge(
        self,
        copy: IsolatedCopy,
        parent: Portal,
        written: Written,
    ) -> MergeReport:
        """把 `copy` 相对基线的改动逐文件写回 `parent`。每次写回都带着读到的父版本做前提：合并期间
        父那边又被改过，这个文件就算冲突，不覆盖。每次写回之后调 `written(写到哪, 内容, 出自副本里
        的哪个文件, 是否混了父的内容)`；删除时内容是 None。"""
        now = _versions_under(copy.location)
        copy.merged_from = now
        merged: list[str] = []
        conflicts: list[MergeConflict] = []
        renamed = _case_renames(copy.base, now)
        for path in sorted(set(copy.base) | set(now)):
            base, child = copy.base.get(path), now.get(path)
            if child == base:
                continue
            if path.casefold() in renamed:
                # 只改了大小写的改名：大小写不敏感的文件系统上，新名字与旧名字是同一个文件，
                # 当成「新建一个、删掉一个」会把唯一的那份删掉。
                reason = '只改了大小写的改名，不自动合并'
                conflicts.append(await self._kept(copy, parent, path, child, reason, written))
                continue
            try:
                current = await parent.stat(path)
                ours = None if current is None else current.version
                if ours == child:
                    merged.append(path)
                elif ours == base:
                    await self._apply(copy, parent, path, child, ours, written)
                    merged.append(path)
                else:
                    reason = await self._both_changed(
                        copy, parent, path, base, child, ours, written
                    )
                    if reason is None:
                        merged.append(path)
                    else:
                        conflicts.append(
                            await self._kept(copy, parent, path, child, reason, written)
                        )
            except WriteConflict:
                reason = '合并期间共享工作区里的这个文件又被改过'
                conflicts.append(await self._kept(copy, parent, path, child, reason, written))
            except (ConfigError, OSError) as exc:
                # 一个写不回去的路径（目录与文件互换、被拒的路径）不能让其余的文件跟着停下：
                # 停在半路的合并再也合不完，每次重试都撞在同一处。
                reason = f'写不回共享工作区（{_cause(exc)}）'
                conflicts.append(await self._kept(copy, parent, path, child, reason, written))
        return MergeReport(
            agent_id=copy.agent_id,
            strategy=copy.strategy,
            merged=tuple(merged),
            conflicts=tuple(conflicts),
            note=copy.note,
        )

    async def _apply(
        self,
        copy: IsolatedCopy,
        parent: Portal,
        path: str,
        child: str | None,
        ours: str | None,
        written: Written,
    ) -> None:
        if child is None:
            await parent.remove(path, expected=ours)
            written(path, None, path, False)
            return
        data = (copy.location / path).read_bytes()
        await parent.write_bytes(path, data, expected=ours)
        written(path, data, path, False)

    async def _both_changed(
        self,
        copy: IsolatedCopy,
        parent: Portal,
        path: str,
        base: str | None,
        child: str | None,
        ours: str | None,
        written: Written,
    ) -> str | None:
        """两边都改过时尝试三方合并，合成了交回 None，否则交回冲突的原因。"""
        if base is None or child is None or ours is None:
            return '一边删了或新建了它，另一边也改过'
        if copy.strategy != 'worktree' or copy.base_dir is None or self._git is None:
            return '两边都改过（快照只做保守合并）'
        theirs = (copy.location / path).read_bytes()
        mine = await parent.read_bytes(path)
        if _version(mine) != ours:
            raise WriteConflict(path, ours, _version(mine))
        result = await self._merge_file((copy.base_dir / path).read_bytes(), mine, theirs)
        if result is None:
            return '两边改了同一处，三方合并合不干净'
        await parent.write_bytes(path, result, expected=ours)
        written(path, result, path, True)
        return None

    async def _kept(
        self,
        copy: IsolatedCopy,
        parent: Portal,
        path: str,
        child: str | None,
        reason: str,
        written: Written,
    ) -> MergeConflict:
        if child is None:
            return MergeConflict(path=path, reason=f'{reason}；子 Agent 删掉了它', removed=True)
        data = (copy.location / path).read_bytes()
        for attempt in range(1, _SIDE_ATTEMPTS + 1):
            suffix = '' if attempt == 1 else f'.{attempt}'
            side = f'{path}.{copy.agent_id}{suffix}.theirs'
            try:
                await parent.write_bytes(side, data, expected=None)
            except WriteConflict:
                continue
            except (ConfigError, OSError) as exc:
                return MergeConflict(path=path, reason=f'{reason}；另存失败（{_cause(exc)}）')
            written(side, data, path, False)
            return MergeConflict(path=path, reason=reason, theirs=side)
        return MergeConflict(path=path, reason=f'{reason}；另存的名字都被占了')

    async def _merge_file(self, base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
        """`git merge-file -p` 的结果；有冲突、不是文本或 git 失败时交回 None。"""
        assert self._git is not None and self._scratch is not None
        try:
            for data in (base, ours, theirs):
                data.decode('utf-8')
        except UnicodeDecodeError:
            return None
        scratch = tempfile.mkdtemp(prefix='syndica-merge-', dir=self._scratch)
        try:
            folder = Path(scratch)
            for name, data in (('ours', ours), ('base', base), ('theirs', theirs)):
                (folder / name).write_bytes(data)
            environment = {
                **{key: value for key, value in self._env.items() if not key.startswith('GIT_')},
                'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_CONFIG_GLOBAL': os.devnull,
                'GIT_CEILING_DIRECTORIES': scratch,
                'HOME': scratch,
            }
            try:
                process = await asyncio.create_subprocess_exec(
                    self._git,
                    'merge-file',
                    '-p',
                    'ours',
                    'base',
                    'theirs',
                    cwd=scratch,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                logger.warning('三方合并起不来 git（%s），这个文件按冲突处理', exc)
                return None
            try:
                async with asyncio.timeout(_MERGE_TIMEOUT):
                    output, _ = await process.communicate()
            except TimeoutError:
                process.kill()
                await process.wait()
                return None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return output if process.returncode == 0 else None


def _working_files(root: Path) -> list[str]:
    files: list[str] = []
    total = 0
    for folder, directories, names in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in _SKIPPED)
        for name in sorted(names):
            path = Path(folder) / name
            if name in _SKIPPED or path.is_symlink() or not path.is_file():
                continue
            files.append(path.relative_to(root).as_posix())
            total += path.stat().st_size
            if len(files) > _MAX_FILES or total > _MAX_BYTES:
                raise ConfigError(
                    expected=(
                        f'要隔离的工作区不超过 {_MAX_FILES} 个文件、'
                        f'{_MAX_BYTES // (1024 * 1024)} MB（.git 与 .syndica 不算）'
                    ),
                    received=f'已经数到 {len(files)} 个文件、{total} 字节',
                    next_action='这件工作用 isolation=shared；或者把依赖与数据集挪出工作区',
                )
    return files


def _differences(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if after.get(path) != before.get(path))


def _case_renames(base: Mapping[str, str], now: Mapping[str, str]) -> frozenset[str]:
    """大小写折叠后撞在一起、且其中有一个是新建或删除的那些名字（折叠后的形式）。两个都一直在
    的（大小写敏感的文件系统上真的是两个文件）照常各自合并。"""
    spellings: dict[str, set[str]] = {}
    for path in set(base) | set(now):
        spellings.setdefault(path.casefold(), set()).add(path)
    return frozenset(
        folded
        for folded, names in spellings.items()
        if len(names) > 1 and any((name in base) != (name in now) for name in names)
    )


def _cause(exc: BaseException) -> str:
    return exc.message if isinstance(exc, ConfigError) else f'{type(exc).__name__}: {exc}'


def _versions_under(location: Path) -> dict[str, str]:
    return {
        relative: _version((location / relative).read_bytes())
        for relative in _working_files(location)
    }


def _version(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'
