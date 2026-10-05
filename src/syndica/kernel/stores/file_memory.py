"""文件 MemoryStore（M6 §8.9）：`root` 下每个 (scope, workspace) 一个目录，短期日志 + 长期记录两份。

布局：`global/` 放全局记忆；`ws-<sha256(workspace)[:16]>/` 放各工作区的。目录里 `short.json`
（短期条目日志）、`long.json`（整理出的长期记录）、`.lock`（跨进程读改写的 flock）。目录 0700、文件
0600。守卫 IO（`O_NOFOLLOW`、`dir_fd`、硬链接核对、上限）见 `guarded_io`；`root` 身份首次打开记下。

写入面只收 `memory_redaction` 铸造过的类型（已脱敏、已挡「不可信进全局」），写盘用 `encode_assured`
保真序列化。`record` 与 `consolidate` 在 `.lock` 上持排他 `flock` 做跨进程 read-modify-write：短期
发号互异、长期乐观并发（`expected_version` 不符报 `MemoryConflict`，不覆盖）。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.memory_redaction import RedactedMemoryEntry, RedactedMemorySections
from syndica.kernel.memory_serde import (
    long_from_json,
    long_to_json,
    short_from_json,
    short_to_json,
)
from syndica.kernel.memory_types import MemoryEntry, MemoryRecord, MemoryScope
from syndica.kernel.redaction import encode_assured
from syndica.kernel.stores import guarded_io as io
from syndica.kernel.stores.memory_memory import MemoryConflict

__all__ = ['FileMemoryStore']

_SHORT = 'short.json'
_LONG = 'long.json'
_LOCK = '.lock'
_LIMIT = 64 * 1024 * 1024


def _keydir(scope: MemoryScope, workspace: str | None) -> str:
    if scope is MemoryScope.GLOBAL:
        return 'global'
    digest = hashlib.sha256((workspace or '').encode('utf-8')).hexdigest()[:16]
    return f'ws-{digest}'


class FileMemoryStore:
    """`root` 是记忆目录的上一级（CLI 传 `<工作区>/.syndica/memory`）。不存在时第一次写建出来。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if isinstance(cast(object, root), bytes) or not isinstance(
            cast(object, root), str | os.PathLike
        ):
            raise ConfigError(
                expected='root 是一个路径（str 或 pathlib.Path）',
                received=f'收到 {type(root).__name__}',
                next_action='传 Path(工作区) / ".syndica" / "memory"',
            )
        self._root = Path(root)
        self._identity: tuple[int, int] | None = None

    async def record(self, entry: RedactedMemoryEntry) -> int:
        inner = entry.entry
        with self._locked_key(inner.scope, inner.workspace) as key:
            entries = self._read_short(key, inner.scope, inner.workspace)
            seq = (entries[-1].seq if entries else 0) + 1
            stored = MemoryEntry(
                seq=seq,
                scope=inner.scope,
                workspace=inner.workspace,
                text=inner.text,
                trust=inner.trust,
                created_at=inner.created_at,
            )
            entries.append(stored)
            self._write(key, _SHORT, short_to_json(inner.scope, inner.workspace, entries))
            return seq

    async def entries(
        self, scope: MemoryScope, workspace: str | None, *, after_seq: int = 0
    ) -> list[MemoryEntry]:
        with self._key_fd(scope, workspace) as key:
            if key is None:
                return []
            entries = self._read_short(key, scope, workspace)
            return [entry for entry in entries if entry.seq > after_seq]

    async def read(self, scope: MemoryScope, workspace: str | None) -> MemoryRecord | None:
        with self._key_fd(scope, workspace) as key:
            if key is None:
                return None
            return self._read_long(key)

    async def consolidate(
        self, sections: RedactedMemorySections, *, expected_version: int
    ) -> int:
        with self._locked_key(sections.scope, sections.workspace) as key:
            current = self._read_long(key)
            version = current.version if current is not None else 0
            if version != expected_version:
                raise MemoryConflict(
                    expected=f'consolidate 时长期记忆版本是 {expected_version}',
                    received=f'当前版本是 {version}（有别的写者先一步整理了）',
                    next_action='重新 read 取最新 version 与 through_seq，合并后再 consolidate',
                )
            record = MemoryRecord(
                scope=sections.scope,
                workspace=None if sections.scope is MemoryScope.GLOBAL else sections.workspace,
                sections=list(sections.sections),
                through_seq=sections.through_seq,
                version=version + 1,
                updated_at=time.time(),
            )
            self._write(key, _LONG, long_to_json(record))
            return version + 1

    # --- internals ------------------------------------------------------------------------

    @contextmanager
    def _root_fd(self) -> Generator[int | None, None, None]:
        try:
            descriptor = io.open_directory(self._root, None, str(self._root))
        except FileNotFoundError:
            yield None
            return
        try:
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if self._identity is None:
                self._identity = identity
            elif identity != self._identity:
                raise ConfigError(
                    expected=f'{self._root} 一直是这个 store 第一次打开的那个目录',
                    received='它被换成了别的目录',
                    next_action='查清是谁动了 .syndica/memory，删掉换进来的那个，重新来',
                )
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _key_fd(
        self, scope: MemoryScope, workspace: str | None
    ) -> Generator[int | None, None, None]:
        name = _keydir(scope, workspace)
        with self._root_fd() as root:
            if root is None or not _is_dir(root, name):
                yield None
                return
            with io.opened(name, root) as key:
                yield key

    @contextmanager
    def _locked_key(
        self, scope: MemoryScope, workspace: str | None
    ) -> Generator[int, None, None]:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = _keydir(scope, workspace)
        with self._root_fd() as root:
            assert root is not None
            try:
                os.mkdir(name, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            with io.opened(name, root) as key:
                lock = os.open(
                    _LOCK,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=key,
                )
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield key
                finally:
                    os.close(lock)

    def _read_short(
        self, key: int, scope: MemoryScope, workspace: str | None
    ) -> list[MemoryEntry]:
        del scope, workspace
        try:
            text = io.read_text(key, _SHORT, f'{self._root}/{_SHORT}', _LIMIT, growing=True)
        except FileNotFoundError:
            return []
        return short_from_json(self._loaded(text, _SHORT))

    def _read_long(self, key: int) -> MemoryRecord | None:
        try:
            text = io.read_text(key, _LONG, f'{self._root}/{_LONG}', _LIMIT, growing=True)
        except FileNotFoundError:
            return None
        return long_from_json(self._loaded(text, _LONG))

    def _loaded(self, text: str, where: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            raise io.corrupt(where, '不是 JSON') from None
        if not isinstance(value, dict):
            raise io.corrupt(where, f'顶层不是对象，是 {type(value).__name__}')
        return cast('dict[str, Any]', value)

    def _write(self, key: int, name: str, value: dict[str, object]) -> None:
        encoded = encode_assured(value)
        temporary = f'.{name}.{uuid.uuid4().hex}.tmp'
        io.write_new(key, temporary, encoded)
        try:
            os.replace(temporary, name, src_dir_fd=key, dst_dir_fd=key)
        except BaseException:
            os.unlink(temporary, dir_fd=key)
            raise
        io.sync_directory(key)


def _is_dir(root: int, name: str) -> bool:
    try:
        mode = os.stat(name, dir_fd=root, follow_symlinks=False).st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode)
