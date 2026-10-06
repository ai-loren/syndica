"""文件 RubricStore（§7.6 / §8.2）：`root` 下每个 scope（会话 id）一个目录，存 goal 与 rubrics。

目录名 `s-<sha256(scope)[:16]>`。目录 0700、文件 0600；守卫 IO 见 `guarded_io`、`root` 身份记下。
写操作在 `.lock` 上持排他 `flock` 做 read-modify-write、读不持锁；写盘用 `encode_assured`（文本已由
`rubric_redaction` 脱敏、类型即脱敏证明）。`put_rubrics` 是 upsert。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from syndica.kernel.acceptance import Goal, Rubric, RubricStatus
from syndica.kernel.errors import ConfigError
from syndica.kernel.redaction import encode_assured
from syndica.kernel.rubric_redaction import RedactedGoal, RedactedRubric
from syndica.kernel.rubric_serde import (
    goal_from_json,
    goal_to_json,
    rubrics_from_json,
    rubrics_to_json,
)
from syndica.kernel.stores import guarded_io as io
from syndica.kernel.stores.memory_rubric import add_proposal, replace_projected

__all__ = ['FileRubricStore']

_GOAL = 'goal.json'
_RUBRICS = 'rubrics.json'
_LOCK = '.lock'
_LIMIT = 64 * 1024 * 1024


def _keydir(scope: str) -> str:
    return f's-{hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]}'


class FileRubricStore:
    """`root` 是 Rubric 目录（CLI 传 `<工作区>/.syndica/rubrics`）。不存在时第一次写建出来。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if isinstance(cast(object, root), bytes) or not isinstance(
            cast(object, root), str | os.PathLike
        ):
            raise ConfigError(
                expected='root 是一个路径（str 或 pathlib.Path）',
                received=f'收到 {type(root).__name__}',
                next_action='传 Path(工作区) / ".syndica" / "rubrics"',
            )
        self._root = Path(root)
        self._identity: tuple[int, int] | None = None

    async def put_goal(self, scope: str, goal: RedactedGoal) -> None:
        with self._locked(scope) as key:
            self._write(key, _GOAL, goal_to_json(goal.goal))

    async def get_goal(self, scope: str) -> Goal | None:
        with self._key_fd(scope) as key:
            if key is None:
                return None
            data = self._read(key, _GOAL)
            return None if data is None else goal_from_json(data)

    async def put_rubrics(self, scope: str, rubrics: list[RedactedRubric]) -> None:
        with self._locked(scope) as key:
            table = {r.id: r for r in self._read_rubrics(key)}
            replace_projected(table, [r.rubric for r in rubrics])
            self._write(key, _RUBRICS, rubrics_to_json(list(table.values())))

    async def rubrics(self, scope: str, *, include_proposed: bool = False) -> list[Rubric]:
        with self._key_fd(scope) as key:
            if key is None:
                return []
            return [
                rubric
                for rubric in self._read_rubrics(key)
                if include_proposed or rubric.status is RubricStatus.ACTIVE
            ]

    async def propose(self, scope: str, rubric: RedactedRubric) -> None:
        with self._locked(scope) as key:
            table = {r.id: r for r in self._read_rubrics(key)}
            add_proposal(table, rubric.rubric)
            self._write(key, _RUBRICS, rubrics_to_json(list(table.values())))

    async def activate(self, scope: str, rubric_id: str) -> None:
        with self._locked(scope) as key:
            table = {r.id: r for r in self._read_rubrics(key)}
            existing = table.get(rubric_id)
            if existing is None or existing.status is not RubricStatus.PROPOSED:
                return
            table[rubric_id] = replace(existing, status=RubricStatus.ACTIVE)
            self._write(key, _RUBRICS, rubrics_to_json(list(table.values())))

    async def forget(self, scope: str) -> None:
        """删掉一个 scope 的 Goal 与全部 Rubric：宿主删会话时调（会话删了，Goal 里那句原话不该还躺在
        磁盘上）。不在 `RubricStore` 协议里：只有宿主删会话，内核从不调它。"""
        with self._locked(scope) as key:
            for name in (_GOAL, _RUBRICS):
                try:
                    os.unlink(name, dir_fd=key)
                except FileNotFoundError:
                    pass
            io.sync_directory(key)

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
                    next_action='查清是谁动了 .syndica/rubrics，删掉换进来的那个，重新来',
                )
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _key_fd(self, scope: str) -> Generator[int | None, None, None]:
        name = _keydir(scope)
        with self._root_fd() as root:
            if root is None or not _is_dir(root, name):
                yield None
                return
            with io.opened(name, root) as key:
                yield key

    @contextmanager
    def _locked(self, scope: str) -> Generator[int, None, None]:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = _keydir(scope)
        with self._root_fd() as root:
            assert root is not None
            try:
                os.mkdir(name, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            with io.opened(name, root) as key:
                lock = os.open(
                    _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=key
                )
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield key
                finally:
                    os.close(lock)

    def _read(self, key: int, name: str) -> dict[str, Any] | None:
        try:
            text = io.read_text(key, name, f'{self._root}/{name}', _LIMIT, growing=True)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            raise io.corrupt(name, '不是 JSON') from None
        if not isinstance(value, dict):
            raise io.corrupt(name, f'顶层不是对象，是 {type(value).__name__}')
        return cast('dict[str, Any]', value)

    def _read_rubrics(self, key: int) -> list[Rubric]:
        data = self._read(key, _RUBRICS)
        return [] if data is None else rubrics_from_json(data)

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
