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


class FileTaint:
    """一个本地工作区的文件污点。构造时取基线；`baseline=False` 不取（隔离副本：它的每个文件由
    父那边的污点逐个登记）。"""

    def __init__(
        self, root: str | Path, *, baseline: bool = True, max_files: int = _MAX_FILES
    ) -> None:
        self._root = Path(root).resolve()
        self._baseline: dict[str, _Fingerprint] = {}
        self._written: dict[str, dict[str, TrustLevel]] = {}
        if baseline:
            self._scan(max_files)

    def trust_of(self, path: str, version: str | None) -> TrustLevel:
        """`path` 内容是 `version` 时可信吗；`version` 为 None 时按它此刻的内容判。"""
        relative = self._relative(path)
        if relative is None:
            return TrustLevel.UNTRUSTED
        if self._unchanged(relative):
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
        if relative is not None and self._unchanged(relative):
            return 'baseline'
        if relative is not None and version in self._written.get(relative, {}):
            return 'tool'
        return 'external'

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
            directories[:] = sorted(name for name in directories if name not in _SKIPPED)
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
        raw = path
        if posixpath.isabs(raw):
            absolute = Path(raw)
            try:
                raw = (absolute.parent.resolve() / absolute.name).relative_to(self._root).as_posix()
            except ValueError:
                return None
        normal = posixpath.normpath(raw)
        if normal in ('.', '') or normal.split('/')[0] == '..':
            return None
        return normal


def _fingerprint(path: Path) -> _Fingerprint | None:
    """普通文件的 (inode, 大小, ctime)；符号链接、目录、不存在的都是 None（按不可信算）。"""
    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return status.st_ino, status.st_size, status.st_ctime_ns
