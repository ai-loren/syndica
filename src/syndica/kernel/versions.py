"""版本层（M5 D4、D5）：git 格式的对象与引用，由内核用 Python 读写，从不调用 git。

`.syndica/vcs` 在工作区里，Agent 的 shell 写得到它的 `config`（过滤器、fsmonitor、`include.path`）。
宿主对它调一次 git，就在危险闸之外执行了 Agent 写下的代码；所以这里只读写对象文件与引用文件，那里
的任何别的文件都不会被读。用户可以 `git --git-dir=.syndica/vcs log` 去看，先看一眼 `config`。

对象落盘之前，正文与提交消息都先过 Redactor（AGENTS.md #8：版本层是一份会活过文件本身的记录）。
脱过敏的版本存的不是原文：它在历史里标明，不能取回原文、不能回滚（回滚会把占位符写进文件）。
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import posixpath
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from syndica.kernel.errors import ConfigError
from syndica.kernel.redaction import Redactor

__all__ = ['VersionEntry', 'VersionStore']

logger = logging.getLogger(__name__)

_REF = 'refs/heads/syndica'
_IDENTITY = 'syndica <syndica@localhost>'
_REDACTED_MARK = 'redacted: content'
_REMOVED_MARK = 'removed: yes'
# 单个文件超过这么大不记版本（会记一条警告）：脱敏扫一遍几百 MB 的数据集会卡住每一次写。
_MAX_RECORDED = 16 * 1024 * 1024
_LOCK_ATTEMPTS = 100
_LOCK_PAUSE = 0.01


@dataclass(frozen=True)
class VersionEntry:
    """一个文件的一次变化。`version` 是原文的内容版本（与 `Portal.stat` 同一种），删除或存的时候
    脱过敏时为 None：那时版本层里没有原文，按版本取不回来。"""

    path: str
    version: str | None
    producer: str
    redacted: bool
    removed: bool
    commit: str


@dataclass(frozen=True)
class _Commit:
    tree: str
    parent: str | None
    message: str


class VersionStore:
    """一个工作区的版本层。方法都是同步的：每次调用里读引用、写对象、改引用之间没有挂起点，同一个
    事件循环里的调用一个接一个地落；别的进程靠引用旁边的锁文件排开。"""

    def __init__(self, root: str | Path, *, redactor: Redactor) -> None:
        self._root = Path(root).resolve()
        self._dir = self._root / '.syndica' / 'vcs'
        self._redactor = redactor

    @property
    def location(self) -> Path:
        return self._dir

    def _checked(self) -> None:
        """`.syndica` 与 `.syndica/vcs` 必须是工作区里的真目录：任何一级是符号链接，对象就写到了
        工作区之外，或写进一个别人随时换得掉的地方。"""
        _require_real_directories(self._root, (self._dir.parent, self._dir))

    def record(self, path: str, data: bytes, *, producer: str) -> VersionEntry | None:
        """记下 `path` 此刻的内容。超过大小上限、或路径本身像密钥（路径照原样进 tree 对象，没法
        脱敏）时不记、交回 None 并记一条警告。"""
        self._checked()
        relative = self._relative(path)
        if self._redactor.redact(relative) != relative:
            logger.warning('版本层没有记下这个文件：它的路径像是含有密钥，这一版不能回滚')
            return None
        if len(data) > _MAX_RECORDED:
            logger.warning(
                '版本层没有记下 %s：%d 字节超过了 %d 字节的上限，这一版不能回滚',
                relative,
                len(data),
                _MAX_RECORDED,
            )
            return None
        stored, redacted = self._redacted(data)
        blob = self._write_object('blob', stored)
        head = self._head()
        if (
            not redacted
            and head is not None
            and self._blob_at(self._read_commit(head).tree, relative) == blob
        ):
            return VersionEntry(
                path=relative,
                version=_version(data),
                producer=producer,
                redacted=False,
                removed=False,
                commit=head,
            )
        mark = _REDACTED_MARK if redacted else ''
        commit = self._commit(relative, blob, producer=producer, mark=mark)
        return VersionEntry(
            path=relative,
            version=None if redacted else _version(data),
            producer=producer,
            redacted=redacted,
            removed=False,
            commit=commit,
        )

    def record_removal(self, path: str, *, producer: str) -> VersionEntry | None:
        """记下 `path` 被删掉了。版本层里本来就没有它时什么也不记，交回 None。"""
        self._checked()
        relative = self._relative(path)
        head = self._head()
        if head is None or self._blob_at(self._read_commit(head).tree, relative) is None:
            return None
        commit = self._commit(relative, None, producer=producer, mark=_REMOVED_MARK)
        return VersionEntry(
            path=relative,
            version=None,
            producer=producer,
            redacted=False,
            removed=True,
            commit=commit,
        )

    def history(self, path: str) -> list[VersionEntry]:
        """`path` 的每一次变化，新的在前。"""
        self._checked()
        relative = self._relative(path)
        entries: list[VersionEntry] = []
        cursor = self._head()
        while cursor is not None:
            commit = self._read_commit(cursor)
            blob = self._blob_at(commit.tree, relative)
            before = (
                None
                if commit.parent is None
                else self._blob_at(self._read_commit(commit.parent).tree, relative)
            )
            # 每次提交只动一个路径，消息第一行写着它：两份原文不同、脱敏后存成同样字节的改动，
            # 按 blob 比看不出来，按提交认得出来。
            touched = commit.message.split('\n', 1)[0].partition(' ')[2] == relative
            redacted = commit.message.rstrip('\n').endswith(_REDACTED_MARK)
            if blob != before or (touched and redacted):
                entries.append(self._entry(relative, cursor, commit, blob))
            cursor = commit.parent
        return entries

    def content(self, path: str, version: str) -> bytes:
        """`path` 在 `version` 时的原文。"""
        self._checked()
        relative = self._relative(path)
        for entry in self.history(relative):
            if entry.version == version:
                blob = self._blob_at(self._read_commit(entry.commit).tree, relative)
                assert blob is not None
                payload = self._read_object(blob, 'blob')
                # history() 先哈希得到版本，content() 再读一次对象：两次之间对象被换掉（Bash 能写进
                # .syndica/vcs，只有文件工具被挡在外），交回的就不是这个版本的内容。按内容复核，
                # 对不上就拒（评审 C11）。
                if _version(payload) != version:
                    raise ConfigError(
                        expected=f'版本 {version} 的对象内容与它的哈希一致',
                        received=f'{path} 在 {version} 的对象读出来是 {_version(payload)}，对不上',
                        next_action='版本库这个对象被改过；别用它恢复，查 .syndica/vcs 是否被改写',
                    )
                return payload
        raise ConfigError(
            expected=f'版本层里有 {relative} 的版本 {version}',
            received=(
                '没有：它可能存的时候脱过敏（原文没有落盘）、超过大小上限没有记，'
                '或者不是经文件工具写的'
            ),
            next_action=(
                f'用 history({relative!r}) 看这个文件记下了哪些版本；脱过敏的版本只能从别处取回原文'
            ),
        )

    def diff(self, path: str, old: str, new: str) -> str:
        """两个版本之间的差异：文本给统一 diff，二进制只说大小。"""
        relative = self._relative(path)
        before, after = self.content(relative, old), self.content(relative, new)
        try:
            lines = (before.decode('utf-8'), after.decode('utf-8'))
        except UnicodeDecodeError:
            return f'{relative} 是二进制文件：{len(before)} 字节 → {len(after)} 字节'
        return ''.join(
            difflib.unified_diff(
                lines[0].splitlines(keepends=True),
                lines[1].splitlines(keepends=True),
                fromfile=f'{relative}@{old}',
                tofile=f'{relative}@{new}',
            )
        )

    def _relative(self, path: str) -> str:
        raw = path
        if posixpath.isabs(raw):
            try:
                raw = str(Path(raw).resolve().relative_to(self._root))
            except ValueError:
                raw = path
        normal = posixpath.normpath(raw.replace(os.sep, '/'))
        if posixpath.isabs(normal) or normal in ('.', '') or normal.split('/')[0] == '..':
            raise ConfigError(
                expected='path 是工作区根内一个文件的路径',
                received=f'path={path!r}',
                next_action='改用相对工作区根的文件路径',
            )
        return normal

    def _redacted(self, data: bytes) -> tuple[bytes, bool]:
        try:
            text, encoding = data.decode('utf-8'), 'utf-8'
        except UnicodeDecodeError:
            # latin-1 一一对应地往返任何字节，二进制文件里的明文密钥同样认得出、换得掉。
            text, encoding = data.decode('latin-1'), 'latin-1'
        clean = self._redactor.redact(text)
        if clean == text:
            return data, False
        return clean.encode(encoding, errors='replace'), True

    def _entry(self, path: str, commit_id: str, commit: _Commit, blob: str | None) -> VersionEntry:
        trailer = commit.message.rstrip('\n').rsplit('\n', 1)[-1]
        redacted = trailer == _REDACTED_MARK
        version = None
        if blob is not None and not redacted:
            version = _version(self._read_object(blob, 'blob'))
        first = commit.message.split('\n', 1)[0]
        return VersionEntry(
            path=path,
            version=version,
            producer=first.split(' ', 1)[0],
            redacted=redacted,
            removed=blob is None,
            commit=commit_id,
        )

    def _commit(self, path: str, blob: str | None, *, producer: str, mark: str) -> str:
        message = self._redactor.redact(f'{producer} {path}')
        if mark:
            message += f'\n\n{mark}'
        with self._locked():
            head = self._head()
            tree = None if head is None else self._read_commit(head).tree
            new_tree = self._with_entry(tree, path.split('/'), blob)
            stamp = int(time.time())
            lines = [f'tree {new_tree}']
            if head is not None:
                lines.append(f'parent {head}')
            lines += [
                f'author {_IDENTITY} {stamp} +0000',
                f'committer {_IDENTITY} {stamp} +0000',
                '',
                message,
                '',
            ]
            commit = self._write_object('commit', '\n'.join(lines).encode('utf-8'))
            self._set_head(commit)
        return commit

    def _with_entry(self, tree: str | None, parts: list[str], blob: str | None) -> str:
        entries = {} if tree is None else self._read_tree(tree)
        name = parts[0]
        if len(parts) == 1:
            if blob is None:
                entries.pop(name, None)
            else:
                entries[name] = ('100644', blob)
        else:
            current = entries.get(name)
            subtree = current[1] if current is not None and current[0] == '40000' else None
            child = self._with_entry(subtree, parts[1:], blob)
            if self._read_tree(child):
                entries[name] = ('40000', child)
            else:
                entries.pop(name, None)
        return self._write_tree(entries)

    def _blob_at(self, tree: str, path: str) -> str | None:
        parts = path.split('/')
        current = tree
        for index, part in enumerate(parts):
            entry = self._read_tree(current).get(part)
            if entry is None:
                return None
            mode, sha = entry
            if index == len(parts) - 1:
                return sha if mode != '40000' else None
            if mode != '40000':
                return None
            current = sha
        return None

    def _read_tree(self, sha: str) -> dict[str, tuple[str, str]]:
        payload = self._read_object(sha, 'tree')
        entries: dict[str, tuple[str, str]] = {}
        index = 0
        while index < len(payload):
            space = payload.index(b' ', index)
            nul = payload.index(b'\0', space)
            mode = payload[index:space].decode('ascii')
            name = payload[space + 1 : nul].decode('utf-8', errors='surrogateescape')
            entries[name] = (mode, payload[nul + 1 : nul + 21].hex())
            index = nul + 21
        return entries

    def _write_tree(self, entries: dict[str, tuple[str, str]]) -> str:
        # git 按名字排序，目录按「名字 + /」参与比较。
        def order(item: tuple[str, tuple[str, str]]) -> bytes:
            name, (mode, _) = item
            encoded = name.encode('utf-8', errors='surrogateescape')
            return encoded + b'/' if mode == '40000' else encoded

        payload = b''.join(
            mode.encode('ascii')
            + b' '
            + name.encode('utf-8', errors='surrogateescape')
            + b'\0'
            + bytes.fromhex(sha)
            for name, (mode, sha) in sorted(entries.items(), key=order)
        )
        return self._write_object('tree', payload)

    def _read_commit(self, sha: str) -> _Commit:
        text = self._read_object(sha, 'commit').decode('utf-8')
        header, _, message = text.partition('\n\n')
        tree = ''
        parent = None
        for line in header.split('\n'):
            key, _, value = line.partition(' ')
            if key == 'tree':
                tree = value
            elif key == 'parent' and parent is None:
                parent = value
        return _Commit(tree=tree, parent=parent, message=message)

    def _object_path(self, sha: str) -> Path:
        return self._dir / 'objects' / sha[:2] / sha[2:]

    def _write_object(self, kind: str, payload: bytes) -> str:
        raw = f'{kind} {len(payload)}\0'.encode('ascii') + payload
        sha = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
        target = self._object_path(sha)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
            temporary.write_bytes(zlib.compress(raw))
            os.replace(temporary, target)
        return sha

    def _read_object(self, sha: str, kind: str) -> bytes:
        try:
            raw = zlib.decompress(self._object_path(sha).read_bytes())
        except (OSError, zlib.error) as exc:
            raise ConfigError(
                expected=f'版本层的对象 {sha} 在且完整',
                received=f'{type(exc).__name__}: {exc}',
                next_action=f'版本层（{self._dir}）被改坏了：删掉整个目录让它从下一次写重新开始记',
            ) from exc
        header, _, payload = raw.partition(b'\0')
        if header.split(b' ', 1)[0] != kind.encode('ascii'):
            raise ConfigError(
                expected=f'对象 {sha} 是 {kind}',
                received=f'它的头是 {header[:32]!r}',
                next_action=f'版本层（{self._dir}）被改坏了：删掉整个目录让它从下一次写重新开始记',
            )
        return payload

    def _head(self) -> str | None:
        ref = self._dir / _REF
        if not ref.is_file():
            return None
        value = ref.read_text(encoding='ascii').strip()
        return value or None

    def _set_head(self, commit: str) -> None:
        head = self._dir / 'HEAD'
        if not head.exists():
            (self._dir / 'refs' / 'tags').mkdir(parents=True, exist_ok=True)
            head.write_text(f'ref: {_REF}\n', encoding='ascii')
            # 版本层在用户的工作区里：不让它出现在 `git status` 里，也不被宿主的自动提交带走。
            (self._dir / '.gitignore').write_text('*\n', encoding='ascii')
        ref = self._dir / _REF
        temporary = ref.with_name(f'.syndica.{os.getpid()}.tmp')
        temporary.write_text(f'{commit}\n', encoding='ascii')
        os.replace(temporary, ref)

    def _locked(self) -> _Lock:
        return _Lock(self._dir / f'{_REF}.lock')


class _Lock:
    """引用旁边的锁文件（与 git 的 `*.lock` 同一种）：两个进程同时改引用时，后到的那个会丢掉先到
    的那次提交。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(_LOCK_ATTEMPTS):
            try:
                self._fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self._fd, str(os.getpid()).encode('ascii'))
                return
            except FileExistsError:
                if self._abandoned():
                    self._path.unlink(missing_ok=True)
                    continue
                time.sleep(_LOCK_PAUSE)
        raise ConfigError(
            expected='版本层的引用锁在一秒内放开',
            received=f'{self._path} 一直在',
            next_action=(
                '另一个进程正在记版本；没有别的 syndica 在跑时，它是一个读不出进程号的残留锁，'
                '删掉它再试'
            ),
        )

    def _abandoned(self) -> bool:
        """锁里记的进程已经不在了（崩溃留下的锁）。读不出进程号时不算：可能正在写。"""
        try:
            pid = int(self._path.read_text(encoding='ascii').strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._path.unlink(missing_ok=True)


def _require_real_directories(root: Path, folders: tuple[Path, ...]) -> None:
    for folder in folders:
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise ConfigError(
                expected=f'{folder.relative_to(root)} 是工作区里的真目录',
                received='它是符号链接，或者不是目录',
                next_action=f'删掉 {folder}；宿主会在下一次用到时重建它',
            )


def _version(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'
