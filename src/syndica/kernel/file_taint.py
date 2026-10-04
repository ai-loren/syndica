"""文件污点（M5 D9）：工作区里一个文件此刻的内容可信吗。

一个版本是 `TRUSTED` 只有两种来路：打开工作区时它就在、此后没动过（基线），或者由一次溯源可信的
工具写入写成。其余的——`Bash` / `Terminal` 写的、外部改的、读过不可信内容之后写的——一律
`UNTRUSTED`。

基线是打开时逐文件取的 (inode, 大小, ctime) 指纹，不哈希内容：ctime 由内核在每次改动时设成当前时间，
用户态写不回去，`touch -d` 伪造修改时间骗不过它；整篇换掉（`mv`、`os.replace`）换了 inode。指纹与
打开时不同就不再按基线算，哪怕内容其实没变（多判的方向，安全）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from syndica.kernel.context import TrustLevel

__all__ = ['FileTaint', 'TaintAware']

logger = logging.getLogger(__name__)

_SKIPPED = frozenset({'.git', '.syndica'})
_MAX_FILES = 20000
# 判一个改过的文件时要哈希它的内容；更大的文件不哈希，按不可信算。
_MAX_HASHED = 64 * 1024 * 1024
_CHUNK = 1024 * 1024

_Fingerprint = tuple[int, int, int]


@runtime_checkable
class TaintAware(Protocol):
    """知道文件污点的 `Portal`（`WorkspaceRouter`）。文件工具按它给读到的内容标信任级别；不实现它
    的后端读到的内容照旧按可信算。"""

    def file_trust(self, path: str, version: str | None) -> TrustLevel: ...

    def name_trust(self, path: str) -> TrustLevel: ...


class FileTaint:
    """一个本地工作区的文件污点。构造时取基线；`baseline=False` 不取（隔离副本：它的每个文件由
    父那边的污点逐个登记）。"""

    def __init__(
        self, root: str | Path, *, baseline: bool = True, max_files: int = _MAX_FILES
    ) -> None:
        self._root = Path(root).resolve()
        self._baseline: dict[str, _Fingerprint] = {}
        self._baseline_dirs: set[str] = set()
        self._baseline_links: dict[str, _Fingerprint] = {}
        self._baseline_hashes: dict[str, str | None] = {}
        self._written: dict[str, dict[str, TrustLevel]] = {}
        if baseline:
            self._scan(max_files)

    def name_trust(self, path: str) -> TrustLevel:
        """列出来的这个名字可信吗（`Glob` / `Grep` 列路径时按它判）。名字也是数据：打开后新建的符号
        链接，名字是攻击者选的，哪怕它指向一个基线文件也不可信——Read 读它的内容仍按内容算（可信），
        这里判的是名字（评审 C2）。路径里非叶子的某一段经过符号链接时保守判不可信。"""
        lexical = posixpath.normpath(path)
        full = self._root / path
        try:
            is_link = full.is_symlink()
        except OSError:
            return TrustLevel.UNTRUSTED
        if is_link:
            recorded = self._baseline_links.get(lexical)
            if recorded is not None and _link_fingerprint(full) == recorded:
                return TrustLevel.TRUSTED
            return TrustLevel.UNTRUSTED
        resolved = self._relative(path)
        if resolved is None or resolved != lexical:
            return TrustLevel.UNTRUSTED
        return self.trust_of(lexical, None)

    def trust_of(self, path: str, version: str | None) -> TrustLevel:
        """`path` 内容是 `version` 时可信吗；`version` 为 None 时按它此刻的内容判。"""
        relative = self._relative(path)
        if relative is None:
            return TrustLevel.UNTRUSTED
        # 基线命中只在问的正是基线那份内容时才算可信：恢复一个旧版本、或经一条换了拼法的路径读到别的
        # 文件，交进来的 version 与基线内容对不上，不能借基线的可信度洗白（phase-3 评审 F1/F4）。
        if self._unchanged(relative) and (
            version is None or version == self._baseline_version(relative)
        ):
            return TrustLevel.TRUSTED
        if version is None and self._is_baseline_dir(relative):
            return TrustLevel.TRUSTED
        known = self._written.get(relative)
        if not known:
            return TrustLevel.UNTRUSTED
        if version is None:
            version = self._current_version(relative)
            if version is None:
                return TrustLevel.UNTRUSTED
        return known.get(version, TrustLevel.UNTRUSTED)

    def source_of(self, path: str, version: str) -> str:
        """`baseline`、`tool` 或 `external`：trace 上标这次读到的版本从哪来。"""
        relative = self._relative(path)
        if relative is None:
            return 'external'
        if self._unchanged(relative) and version == self._baseline_version(relative):
            return 'baseline'
        if version in self._written.get(relative, {}):
            return 'tool'
        return 'external'

    def script_trust(self, path: str) -> TrustLevel | None:
        """执行 `path` 这个脚本时它此刻可信吗。不在工作区里（宿主自己的程序 `/usr/bin/x`、工作区外的
        `../x`）交回 None：那不是工作区的内容，按不可信算会让每条写全路径的命令升级（评审 F3）。"""
        if self._relative(path) is None:
            return None
        return self.trust_of(path, None)

    def wrote(self, path: str, version: str, trust: TrustLevel) -> None:
        """一次经路由的写把 `path` 写成了 `version`，那次调用的溯源结论是 `trust`。同一份内容被写过
        两次时取严：内容一样，可信度不因为后一次写的人可信而变好。"""
        relative = self._relative(path)
        if relative is None:
            return
        versions = self._written.setdefault(relative, {})
        before = versions.get(version)
        versions[version] = (
            TrustLevel.UNTRUSTED if TrustLevel.UNTRUSTED in (before, trust) else TrustLevel.TRUSTED
        )

    def _scan(self, max_files: int) -> None:
        for folder, directories, names in os.walk(self._root):
            # 符号链接（文件或目录）按名字记进基线：打开时就在的链接名字可信，之后新建的不可信。
            for entry in (*directories, *names):
                path = Path(folder) / entry
                if path.is_symlink():
                    link = _link_fingerprint(path)
                    if link is not None:
                        self._baseline_links[path.relative_to(self._root).as_posix()] = link
            directories[:] = sorted(name for name in directories if name not in _SKIPPED)
            relative_folder = Path(folder).relative_to(self._root).as_posix()
            if relative_folder != '.':
                self._baseline_dirs.add(relative_folder)
            for name in sorted(names):
                if len(self._baseline) >= max_files:
                    logger.warning(
                        '工作区超过 %d 个文件，文件污点的基线只取了前 %d 个：'
                        '其余文件读到时按不可信算',
                        max_files,
                        max_files,
                    )
                    return
                path = Path(folder) / name
                fingerprint = _fingerprint(path)
                if fingerprint is not None:
                    self._baseline[path.relative_to(self._root).as_posix()] = fingerprint

    def _unchanged(self, relative: str) -> bool:
        recorded = self._baseline.get(relative)
        return recorded is not None and _fingerprint(self._root / relative) == recorded

    def _is_baseline_dir(self, relative: str) -> bool:
        """打开时就在、此刻仍是目录的那个目录。目录名本身是基线内容，不要因为它没有文件指纹就判不可信
        （phase-3 评审 F7：`Glob('*')` 把基线目录列出来会整条结果变成不可信）。"""
        return relative in self._baseline_dirs and (self._root / relative).is_dir()

    def _baseline_version(self, relative: str) -> str | None:
        """基线内容的版本（哈希）：懒算并缓存。只在 `_unchanged` 成立时调用，此刻文件内容即基线，
        所以现在哈希出来的就是基线那份；文件一旦动过 `_unchanged` 即为假，这份缓存不会再被读到。"""
        if relative not in self._baseline_hashes:
            self._baseline_hashes[relative] = self._current_version(relative)
        return self._baseline_hashes[relative]

    def _current_version(self, relative: str) -> str | None:
        path = self._root / relative
        try:
            if path.is_symlink() or path.stat().st_size > _MAX_HASHED:
                return None
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
        except OSError:
            return None
        return f'sha256:{digest.hexdigest()}'

    def _relative(self, path: str) -> str | None:
        # 按后端解析路径的同一方式（`Path.resolve()`）折叠：先跟符号链接、再吃 `..`，否则一条经过
        # 伪造链接的 `sub/../README.md` 会按字面落到 `README.md` 上，借那个基线文件的可信度洗白
        # 读到的别处内容（phase-3 评审 F1）。
        raw = Path(path)
        candidate = raw if raw.is_absolute() else self._root / raw
        try:
            resolved = candidate.resolve()
            return resolved.relative_to(self._root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None


def _fingerprint(path: Path) -> _Fingerprint | None:
    """普通文件的 (inode, 大小, ctime)；符号链接、目录、不存在的都是 None（按不可信算）。"""
    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return status.st_ino, status.st_size, status.st_ctime_ns


def _link_fingerprint(path: Path) -> _Fingerprint | None:
    """符号链接自己的 (inode, 大小, ctime)：大小是目标路径的字节数，改指向就变；不是链接是 None。"""
    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISLNK(status.st_mode):
        return None
    return status.st_ino, status.st_size, status.st_ctime_ns
