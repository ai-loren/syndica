"""文件 SessionStore（M6 T2）：`root` 下一个会话一个目录。语义见 `protocols.session`。

布局：`<sid>/meta.json`（元数据，`load_meta` / `list_metas` 只读它，正文可达数十兆不进内存）、
`<sid>/record.json`（整份记录，`load` 读它、它是正文的权威）、`<sid>/archive/<seq>.json`（归档）。
目录 0700、文件 0600。守卫 IO（`O_NOFOLLOW`、`dir_fd`、硬链接 / FIFO 核对、上限）见 `guarded_io`。
`root` 记下第一次打开时的身份（设备与 inode），此后换成别的目录就拒绝。`root` 的上级由宿主核对。

同步 IO：同一个事件循环里并发的 `save` / `archive` 之间没有挂起点，因此天然串行（last-writer、序号
互异）。跨进程不做保证——宿主在 resume 时用锁文件挡住并发恢复同一个会话。

写入面只收 `RedactedSessionRecord` / `RedactedContent`：正文已逐叶脱敏。这里写盘不再脱敏、也不排
序键，用 `encode_assured` 保真序列化（类型即脱敏证明）——重脱会打乱 extra 键序、把幂等键改坏。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.redaction import encode_assured
from syndica.kernel.session_redaction import RedactedContent, RedactedSessionRecord
from syndica.kernel.session_serde import (
    meta_from_json,
    meta_to_json,
    record_from_json,
    record_to_json,
)
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus
from syndica.kernel.stores import guarded_io as io

__all__ = ['FileSessionStore']

_META = 'meta.json'
_RECORD = 'record.json'
_ARCHIVE = 'archive'
# 每个会话只留最近这么多份归档：每次压缩归档一份完整前像（spec §8.8），跑几天的会话会一直长下去。
_ARCHIVE_KEEP = 20
# 元数据只有几 KB；整份记录给足余量，只挡住把宿主内存撑爆的那种。
_META_LIMIT = 4 * 1024 * 1024
_RECORD_LIMIT = 512 * 1024 * 1024


def _valid_sid(sid: str) -> str:
    # sid 是目录名：不许空、不许带路径分隔或 `.`，否则会走出 root。NUL / 换行 / 过长会让底层 open
    # 抛裸 OSError / ValueError，这里换成三件套（255 是常见文件名上限）。
    if (
        not isinstance(cast(object, sid), str)
        or not sid
        or '/' in sid
        or sid in ('.', '..')
        or '\x00' in sid
        or '\n' in sid
        or '\r' in sid
        or len(sid.encode('utf-8')) > 255
    ):
        raise ConfigError(
            expected='sid 是一个非空、不含 / . 换行与 NUL、不超过 255 字节的会话 id',
            received=f'收到 sid={sid!r}',
            next_action='用 run / chat 生成的会话 id（它是根 Agent 的 id）',
        )
    return sid


class FileSessionStore:
    """`root` 是会话目录的上一级（CLI 传 `<工作区>/.syndica/sessions`）。不存在时第一次写建出来
    （0700）；它自己不许是符号链接。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if isinstance(cast(object, root), bytes) or not isinstance(
            cast(object, root), str | os.PathLike
        ):
            raise ConfigError(
                expected='root 是一个路径（str 或 pathlib.Path）',
                received=f'收到 {type(root).__name__}',
                next_action='传 Path(工作区) / ".syndica" / "sessions"',
            )
        self._root = Path(root)
        self._identity: tuple[int, int] | None = None

    async def exists(self, sid: str) -> bool:
        _valid_sid(sid)
        # 按最后写的 meta.json 判存在，不按目录：第一次存盘在写 record.json 时失败（磁盘满 / 权限 /
        # 进程中途死）会留下半截目录，按目录判会让 exists 说有、而 load 取不回，并让此后每次存盘都先
        # 在 load_meta 上炸、永远存不进去（F2）。meta 在 record 之后写，有它即两者都在。
        with self._session_fd(sid) as session:
            return session is not None and self._has_file(session, _META)

    async def load_meta(self, sid: str) -> SessionMeta | None:
        _valid_sid(sid)
        with self._session_fd(sid) as session:
            if session is None:
                return None
            try:
                return self._read_meta(session, sid)
            except FileNotFoundError:
                return None

    async def load(self, sid: str) -> SessionRecord | None:
        _valid_sid(sid)
        with self._session_fd(sid) as session:
            if session is None:
                return None
            try:
                record = self._read_record(session, sid)
            except FileNotFoundError:
                return None
            # meta.json 是 live 元数据的权威，archive 只前移它。读 record 时拿 meta.json
            # 覆盖内嵌那份（F6），否则 archived 落后、随后写又回滚。归档副本不覆盖。
            try:
                record.meta = self._read_meta(session, sid)
            except FileNotFoundError:
                pass
            return record

    async def save(self, sid: str, record: RedactedSessionRecord) -> None:
        _valid_sid(sid)
        inner = record.record
        if inner.meta.sid != sid:
            raise ContractViolation(
                expected=f'record.meta.sid 与 save 的 sid 一致（都是 {sid!r}）',
                received=f'record.meta.sid={inner.meta.sid!r}，与 {sid!r} 不符',
                next_action='它们指同一会话；对齐两者，否则记录按一个 id 存、按另一个 id 找不回',
            )
        with self._created_session(sid) as session:
            self._write_json(session, _RECORD, record_to_json(inner))
            self._write_json(session, _META, meta_to_json(inner.meta))

    async def archive(self, sid: str, record: RedactedSessionRecord) -> int:
        _valid_sid(sid)
        if record.record.meta.sid != sid:
            raise ContractViolation(
                expected=f'record.meta.sid 与 archive 的 sid 一致（都是 {sid!r}）',
                received=f'record.meta.sid={record.record.meta.sid!r}，与 {sid!r} 不符',
                next_action='它们指同一会话；对齐两者，否则归档按一个 id 存、按另一个 id 找不回',
            )
        with self._created_session(sid) as session:
            seq = self._next_seq(session, sid)
            snapshot = record_to_json(record.record)
            snapshot['meta'] = {
                **cast('dict[str, object]', snapshot['meta']),
                'status': SessionStatus.ARCHIVED.value,
                'archived': seq,
            }
            if not self._has_archive(session):
                os.mkdir(_ARCHIVE, mode=0o700, dir_fd=session)
            with io.opened(_ARCHIVE, session) as archive:
                self._write_json(archive, f'{seq}.json', snapshot)
                _prune_archives(archive, keep_after=seq - _ARCHIVE_KEEP)
            self._bump_archived(session, sid, seq)
            return seq

    async def list_metas(
        self,
        *,
        status: SessionStatus | None = None,
        agent: str | None = None,
        workspace: str | None = None,
    ) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        with self._root_fd() as root:
            if root is None:
                return []
            for meta in self._metas(root):
                if (
                    (status is None or meta.status is status)
                    and (agent is None or meta.agent == agent)
                    and (workspace is None or meta.workspace == workspace)
                ):
                    metas.append(meta)
        metas.sort(key=lambda meta: meta.updated_at, reverse=True)
        return metas

    async def unreadable(self) -> list[tuple[str, str, str]]:
        """有 `meta.json` 却读不出来、因而不在 `list_metas()` 里的会话：（sid，怎么了，下一步）。
        没有 `meta.json` 的目录不算：那是第一次存盘半途失败留下的，这个会话本来就不存在。不在
        `SessionStore` 协议里：别的 store 没有文件可言。"""
        with self._root_fd() as root:
            if root is None:
                return []
            skipped: list[tuple[str, str, str]] = []
            for _ in self._metas(root, skipped=skipped):
                pass
            return skipped

    async def replace_content(self, sid: str, content: RedactedContent) -> None:
        _valid_sid(sid)
        with self._session_fd(sid) as session:
            if session is None:
                raise ContractViolation(
                    expected=f'会话 {sid!r} 已存在',
                    received='存储里没有这个会话',
                    next_action='先 save 再 replace_content，或检查 sid 是否拼错',
                )
            record = self._read_record(session, sid)
            # 用 meta.json（权威）覆盖内嵌 meta，否则把 archive 前移过的 archived 写回成旧值（F6）。
            record.meta = self._read_meta(session, sid)
            record.content = content.content
            record.meta.updated_at = time.time()
            self._write_json(session, _RECORD, record_to_json(record))
            self._write_json(session, _META, meta_to_json(record.meta))

    async def delete(self, sid: str) -> None:
        _valid_sid(sid)
        with self._root_fd() as root:
            if root is None or not self._is_session_dir(root, sid):
                return
            shutil.rmtree(sid, dir_fd=root)

    async def read_archived(self, sid: str, seq: int) -> SessionRecord | None:
        _valid_sid(sid)
        with self._session_fd(sid) as session:
            if session is None or not self._has_archive(session):
                return None
            with io.opened(_ARCHIVE, session) as archive:
                try:
                    return self._read_json_record(
                        archive, f'{seq}.json', f'{self._root / sid}/{_ARCHIVE}/{seq}.json'
                    )
                except FileNotFoundError:
                    return None

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
                raise ContractViolation(
                    expected=f'{self._root} 一直是这个 store 第一次打开的那个目录',
                    received='它被换成了别的目录（上级换成了符号链接，或者挪走之后又建了一个）',
                    next_action='查清是谁动了 .syndica，删掉换进来的那个，重新来',
                )
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _created_session(self, sid: str) -> Generator[int, None, None]:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._root_fd() as root:
            if root is None:
                raise ContractViolation(
                    expected=f'{self._root} 建出来之后还在',
                    received='刚建好它就不见了',
                    next_action='检查是不是别的进程在同时删它，然后重新来',
                )
            try:
                os.mkdir(sid, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            with io.opened(sid, root) as session:
                yield session

    @contextmanager
    def _session_fd(self, sid: str) -> Generator[int | None, None, None]:
        with self._root_fd() as root:
            if root is None or not self._is_session_dir(root, sid):
                yield None
                return
            with io.opened(sid, root) as session:
                yield session

    def _metas(
        self, root: int, *, skipped: list[tuple[str, str, str]] | None = None
    ) -> Iterator[SessionMeta]:
        """每个会话目录的元数据。读不出来的跳过，给了 `skipped` 就记进去：一个坏掉的会话不能让别的
        会话都列不出来。"""
        for name in sorted(os.listdir(root)):
            if not self._is_session_dir(root, name):
                continue
            where = f'{self._root / name}/{_META}'
            with io.opened(name, root) as session:
                try:
                    meta = self._read_meta(session, name)
                except FileNotFoundError:
                    continue
                except (ContractViolation, ConfigError) as error:
                    if skipped is not None:
                        skipped.append(
                            (name, f'{where} 读不进来：{error.received}', error.next_action)
                        )
                    continue
                except OSError as error:
                    if skipped is not None:
                        skipped.append(
                            (
                                name,
                                f'{where} 读不进来：{type(error).__name__}: {error.strerror}',
                                f'查清 {where} 为什么读不了（权限、属主），修好再来',
                            )
                        )
                    continue
            yield meta

    def _is_session_dir(self, root: int, name: str) -> bool:
        if name in ('.', '..') or '/' in name:
            return False
        try:
            mode = os.stat(name, dir_fd=root, follow_symlinks=False).st_mode
        except OSError:
            return False
        return stat.S_ISDIR(mode)

    def _has_archive(self, session: int) -> bool:
        try:
            mode = os.stat(_ARCHIVE, dir_fd=session, follow_symlinks=False).st_mode
        except OSError:
            return False
        return stat.S_ISDIR(mode)

    def _has_file(self, session: int, name: str) -> bool:
        try:
            mode = os.stat(name, dir_fd=session, follow_symlinks=False).st_mode
        except OSError:
            return False
        return stat.S_ISREG(mode)

    def _next_seq(self, session: int, sid: str) -> int:
        # 取 meta.archived 与现存归档文件名的最大值 +1。只看文件名：删掉最新归档文件（目录
        # Agent 可写）会让下次重发同一个 seq，trace 引过的 seq 此后指向别的快照（F6）。只增不补。
        highest = 0
        try:
            highest = self._read_meta(session, sid).archived
        except (OSError, ContractViolation, ConfigError):
            pass
        if self._has_archive(session):
            for name in self._archive_names(session):
                highest = max(highest, int(name.removesuffix('.json')))
        return highest + 1

    def _archive_names(self, session: int) -> Iterator[str]:
        with io.opened(_ARCHIVE, session) as archive:
            for name in os.listdir(archive):
                stem = name.removesuffix('.json')
                # isascii 挡 '²'：isdigit() 真但 int() 抛 ValueError，会让此后 archive 全失败。
                if name.endswith('.json') and stem.isascii() and stem.isdigit():
                    yield name

    def _bump_archived(self, session: int, sid: str, seq: int) -> None:
        try:
            meta = self._read_meta(session, sid)
        except (OSError, ContractViolation, ConfigError):
            return
        meta.archived = seq
        self._write_json(session, _META, meta_to_json(meta))

    def _read_meta(self, session: int, sid: str) -> SessionMeta:
        text = io.read_text(session, _META, f'{self._root / sid}/{_META}', _META_LIMIT)
        return meta_from_json(self._loaded(text, f'{self._root / sid}/{_META}'))

    def _read_record(self, session: int, sid: str) -> SessionRecord:
        return self._read_json_record(session, _RECORD, f'{self._root / sid}/{_RECORD}')

    def _read_json_record(self, directory: int, name: str, where: str) -> SessionRecord:
        text = io.read_text(directory, name, where, _RECORD_LIMIT, growing=True)
        return record_from_json(self._loaded(text, where))

    def _loaded(self, text: str, where: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            raise io.corrupt(where, '不是 JSON') from None
        if not isinstance(value, dict):
            raise io.corrupt(where, f'顶层不是对象，是 {type(value).__name__}')
        return cast('dict[str, Any]', value)

    def _write_json(self, directory: int, name: str, value: dict[str, object]) -> None:
        """原子写：写一个临时文件再 `os.replace` 盖上去，崩溃时要么是旧的要么是新的，不留半截。

        记录已在收口（`redacted_record` / `redacted_content`）逐叶脱过敏，这里只做保真序列化：
        `encode_assured` 不排序、不重脱——否则 `extra` 键序丢失、内核幂等键 `syndica.key` 被当凭证
        改掉（见 redaction.encode_assured）。
        """
        encoded = encode_assured(value)
        temporary = f'.{name}.{uuid.uuid4().hex}.tmp'
        io.write_new(directory, temporary, encoded)
        try:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            os.unlink(temporary, dir_fd=directory)
            raise
        io.sync_directory(directory)


def _prune_archives(archive: int, *, keep_after: int) -> None:
    """删掉序号不大于 `keep_after` 的归档（`read_archived` 对它们交回 None）。"""
    if keep_after <= 0:
        return
    for name in os.listdir(archive):
        stem, dot, ext = name.partition('.')
        if dot and ext == 'json' and stem.isdigit() and int(stem) <= keep_after:
            os.unlink(name, dir_fd=archive)
