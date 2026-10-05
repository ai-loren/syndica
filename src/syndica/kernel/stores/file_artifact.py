"""文件 ArtifactStore（spec §4.4 / §7.2）：`root` 下一份 `index.json` 存 id → 版本列表 + 交付标记。

只存路径 / 版本元数据，产物正文在工作区里（§6），没有正文要脱敏。目录 0700、文件 0600。守卫 IO
（`O_NOFOLLOW`、`dir_fd`、硬链接核对、上限）见 `guarded_io`；`root` 身份首次打开记下。`register`
在 `.lock` 上持排他 `flock` 做跨进程 read-modify-write；读不持锁。写盘用 `encode_assured`（无正文、
类型即脱敏证明）。
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from syndica.kernel.artifact_serde import index_from_json, index_to_json
from syndica.kernel.context import ArtifactRef
from syndica.kernel.errors import ConfigError
from syndica.kernel.redaction import encode_assured
from syndica.kernel.stores import guarded_io as io

__all__ = ['FileArtifactStore']

_INDEX = 'index.json'
_LOCK = '.lock'
_LIMIT = 64 * 1024 * 1024


class FileArtifactStore:
    """`root` 是产物索引目录（CLI 传 `<工作区>/.syndica/artifacts`）。不存在时第一次写建出来。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if isinstance(cast(object, root), bytes) or not isinstance(
            cast(object, root), str | os.PathLike
        ):
            raise ConfigError(
                expected='root 是一个路径（str 或 pathlib.Path）',
                received=f'收到 {type(root).__name__}',
                next_action='传 Path(工作区) / ".syndica" / "artifacts"',
            )
        self._root = Path(root)
        self._identity: tuple[int, int] | None = None

    async def register(self, ref: ArtifactRef) -> None:
        with self._locked() as key:
            order, versions = self._read(key)
            existing = versions.get(ref.id)
            if existing is None:
                order.append(ref.id)
                versions[ref.id] = [ref]
            elif any(v.version == ref.version for v in existing):
                return  # 同 (id, version) 幂等：保留先登记的那条（§7.2）
            else:
                existing.append(ref)
            self._write(key, index_to_json(order, versions))

    async def get(self, artifact_id: str) -> ArtifactRef | None:
        _, versions = self._read_only()
        chain = versions.get(artifact_id)
        return chain[-1] if chain else None

    async def versions_of(self, artifact_id: str) -> list[ArtifactRef]:
        _, versions = self._read_only()
        return list(versions.get(artifact_id, ()))

    async def list(
        self,
        *,
        kind: str | None = None,
        producer: str | None = None,
        deliverable_only: bool = False,
    ) -> list[ArtifactRef]:
        order, versions = self._read_only()
        out: list[ArtifactRef] = []
        for artifact_id in order:
            chain = versions[artifact_id]
            latest = chain[-1]
            if kind is not None and latest.kind != kind:
                continue
            if producer is not None and latest.producer != producer:
                continue
            if deliverable_only and not any(v.is_deliverable for v in chain):
                continue
            out.append(latest)
        return out

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
                    next_action='查清是谁动了 .syndica/artifacts，删掉换进来的那个，重新来',
                )
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self) -> Generator[int, None, None]:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._root_fd() as root:
            assert root is not None
            lock = os.open(
                _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root
            )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield root
            finally:
                os.close(lock)

    def _read_only(self) -> tuple[list[str], dict[str, list[ArtifactRef]]]:
        with self._root_fd() as root:
            if root is None:
                return [], {}
            return self._read(root)

    def _read(self, root: int) -> tuple[list[str], dict[str, list[ArtifactRef]]]:
        try:
            text = io.read_text(root, _INDEX, f'{self._root}/{_INDEX}', _LIMIT, growing=True)
        except FileNotFoundError:
            return [], {}
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            raise io.corrupt(_INDEX, '不是 JSON') from None
        if not isinstance(value, dict):
            raise io.corrupt(_INDEX, f'顶层不是对象，是 {type(value).__name__}')
        return index_from_json(cast('dict[str, Any]', value))

    def _write(self, root: int, value: dict[str, object]) -> None:
        encoded = encode_assured(value)
        temporary = f'.{_INDEX}.{uuid.uuid4().hex}.tmp'
        io.write_new(root, temporary, encoded)
        try:
            os.replace(temporary, _INDEX, src_dir_fd=root, dst_dir_fd=root)
        except BaseException:
            os.unlink(temporary, dir_fd=root)
            raise
        io.sync_directory(root)
