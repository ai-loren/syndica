"""宿主 git 环境（spec D14）：工作树的改动与每个文件的 blob hash。

快照、状态、hash、提交、推送都在同一个环境里跑：宿主自己的环境（含 ssh agent）加
`GIT_TERMINAL_PROMPT=0`（要凭证时失败，不卡在等输入上）、`GIT_LITERAL_PATHSPECS=1`（`*`、`:!x`
这样的文件名只匹配它们自己）与 `GIT_OPTIONAL_LOCKS=0`（取快照不改写 `.git/index`）。每条都加
`-c core.fsmonitor=false`（仓库配置里的 fsmonitor 是一条任意命令，会以宿主的环境执行）与
`--attr-source=HEAD`（工作树里 Agent 写的 `.gitattributes` 选不中配置里的 filter 驱动）。以参数
数组直接调 git，不经 shell；stdin 接 `/dev/null`。复核命令不在这里跑：它们验证的是 Agent 能跑出
的东西，在 Agent 的环境里跑。需要 git 2.40 以上（`--attr-source`）。
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

__all__ = ['LOCAL_TIMEOUT', 'Change', 'GitUnavailable', 'HostGit', 'host_environment']

LOCAL_TIMEOUT = 60.0
# `.syndica/` 里是宿主自己的产物（handoff、锁），不是本次 run 的改动。
_OWN = '.syndica'
# 一次 `hash-object` 交多少个路径：参数表有长度上限。
_HASH_BATCH = 200
# 宿主环境里这些变量会把 git 指到别的仓库、别的索引或注入配置：从 git 钩子或包装脚本里启动时它们
# 都在。去掉它们，git 只按工作区找仓库。
_REDIRECTING = re.compile(
    r'\AGIT_(DIR|WORK_TREE|INDEX_FILE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|COMMON_DIR'
    r'|NAMESPACE|PREFIX|CONFIG_PARAMETERS|CONFIG_COUNT|CONFIG_KEY_\d+|CONFIG_VALUE_\d+)\Z'
)


class GitUnavailable(Exception):
    """git 用不了：没装、不是 git 仓库、命令失败或超时、工作区里有指纹取不到的东西。消息是一整句，
    含下一步。"""


@dataclass(frozen=True)
class Change:
    """`git status --porcelain=v1` 的一条，路径相对工作区根。`blob` 是工作树里那份内容的 blob id
    （与提交后的一致：过 clean filter，符号链接按链接文本），文件已删时是 None；`mode` 是工作树里
    那个文件的类型与可执行位（`100644` / `100755` / `120000`，已删是 None）——只改权限、把文件换成
    同文本的符号链接，blob 都不变。改名与复制的 `path` 是新路径，`origin` 是旧路径。"""

    path: str
    status: str
    blob: str | None
    origin: str | None = None
    mode: str | None = None


def host_environment(environ: Mapping[str, str]) -> dict[str, str]:
    kept = {name: value for name, value in environ.items() if not _REDIRECTING.match(name)}
    return {
        **kept,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_LITERAL_PATHSPECS': '1',
        'GIT_OPTIONAL_LOCKS': '0',
    }


def _entries(raw: bytes) -> list[tuple[str, str, str | None]]:
    """解析 `-z` 格式：`XY 路径` 以 NUL 结尾，改名与复制后面再跟一个旧路径。路径按 surrogateescape
    解码：交回给 git 时原样编码回去，不是 UTF-8 的文件名也不丢。"""
    fields = raw.decode('utf-8', 'surrogateescape').split('\0')
    parsed: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(fields) and fields[index]:
        record = fields[index]
        status, path = record[:2], record[3:]
        origin = None
        if 'R' in status or 'C' in status:
            index += 1
            origin = fields[index]
        parsed.append((status, path, origin))
        index += 1
    return parsed


def _own(path: str | None) -> bool:
    return path is not None and (path == _OWN or path.startswith(f'{_OWN}/'))


def _mode(full: Path) -> str | None:
    try:
        info = os.lstat(full)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return '120000'
    return '100755' if info.st_mode & stat.S_IXUSR else '100644'


class HostGit:
    def __init__(self, root: Path, environ: Mapping[str, str]) -> None:
        self.root = root
        self._env = host_environment(environ)
        self._object_format: str | None = None
        self._prefix: str | None = None

    @property
    def environment(self) -> Mapping[str, str]:
        """宿主 git 环境本身：提交要从 stdin 给消息，在这一层之外起子进程时用同一份环境。"""
        return MappingProxyType(self._env)

    def run(self, *args: str, timeout: float = LOCAL_TIMEOUT) -> bytes:
        """跑一条 git，交回 stdout；退出码不是 0 也是 GitUnavailable。"""
        completed = self.attempt(*args, timeout=timeout)
        if completed.returncode != 0:
            detail = completed.stderr.decode('utf-8', 'replace').strip() or '（没有输出）'
            raise GitUnavailable(
                f'期望 `git {args[0]}` 成功，实际退出 {completed.returncode}：{detail}。确认 '
                f'{self.root} 在 git 仓库里、宿主的 git 是 2.40 以上、没有别的进程正在改动工作区，'
                '再重试。'
            )
        return completed.stdout

    def attempt(
        self, *args: str, timeout: float = LOCAL_TIMEOUT
    ) -> subprocess.CompletedProcess[bytes]:
        """跑一条可以失败的 git，交回完整结果（退出码与两条流，推送要按 stderr 分辨失败原因）；
        起不来（工作区不是目录、没有 git、超时、系统拒绝）仍是 GitUnavailable。"""
        if not self.root.is_dir():
            raise GitUnavailable(
                f'期望工作区 {self.root} 是一个存在的目录，实际它不存在或不是目录。确认工作区路径'
                '没写错、没被删掉。'
            )
        try:
            return subprocess.run(
                ['git', '-c', 'core.fsmonitor=false', '--attr-source=HEAD', *args],
                cwd=self.root,
                env=self._env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitUnavailable(
                '期望宿主上有 git，实际找不到 git 可执行文件。装上 git，或把它所在的目录加进 PATH。'
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitUnavailable(
                f'期望 `git {args[0]}` 在 {timeout:g} 秒内结束，实际超时。确认没有别的 git 进程占着'
                '这个仓库、也没有进程在大量改动工作区，再重试。'
            ) from exc
        except OSError as exc:
            raise GitUnavailable(
                f'期望能在 {self.root} 里运行 `git {args[0]}`，实际系统拒绝了：{exc}。按这个原因'
                '修好之后重试。'
            ) from exc

    def prefix(self) -> str:
        """工作区相对 git 顶层目录的前缀（`sub/dir/`），就在顶层时是空串。"""
        if self._prefix is None:
            self._prefix = self.run('rev-parse', '--show-prefix').decode('utf-8', 'surrogateescape')
            self._prefix = self._prefix.strip('\n')
        return self._prefix

    def changes(self) -> tuple[Change, ...]:
        """工作区里相对 HEAD 与索引的改动（含未跟踪、不含被忽略的与 `.syndica/`），路径相对工作区
        根、按路径排序。工作区可以在仓库的子目录里：只看这个目录之下。"""
        prefix = self.prefix()
        raw = self.run('status', '--porcelain=v1', '-z', '--untracked-files=all', '--', '.')
        entries: list[tuple[str, str, str | None]] = []
        for status, path, origin in _entries(raw):
            relative = path[len(prefix) :] if path.startswith(prefix) else path
            if origin is not None:
                origin = posixpath.relpath(origin, prefix or '.')
            # 改名跨过 `.syndica/` 的边界时只看工作区这一侧：挪进去等于原文件被删，挪出来等于
            # 新增一个文件。整条丢掉，原文件被删这件事就看不见了。
            if _own(relative) and origin is not None and not _own(origin):
                entries.append(('D ', origin, None))
            elif _own(origin) and not _own(relative):
                entries.append(('A ', relative, None))
            elif not _own(relative):
                entries.append((status, relative, origin))
        nested = sorted(
            path
            for _, path, _ in entries
            if (self.root / path).is_dir() and not (self.root / path).is_symlink()
        )
        if nested:
            raise GitUnavailable(
                f'期望改动里都是文件，实际有嵌套仓库或子模块：{", ".join(nested)}。它们里面的改动'
                '取不到指纹；把它们挪出工作区、提交它们，或加进 .gitignore 之后重跑。'
            )
        blobs = self._blobs([path for _, path, _ in entries])
        found = [
            Change(path, status, blobs.get(path), origin, _mode(self.root / path))
            for status, path, origin in entries
        ]
        return tuple(sorted(found, key=lambda change: change.path))

    def _blobs(self, paths: Sequence[str]) -> dict[str, str]:
        files: list[str] = []
        blobs: dict[str, str] = {}
        for path in paths:
            full = self.root / path
            if full.is_symlink():
                blobs[path] = self._blob_id(os.readlink(full).encode('utf-8', 'surrogateescape'))
            elif full.is_file():
                files.append(path)
        for start in range(0, len(files), _HASH_BATCH):
            batch = files[start : start + _HASH_BATCH]
            hashes = self.run('hash-object', '--', *batch).decode('ascii').split()
            blobs.update(zip(batch, hashes, strict=True))
        return blobs

    def _blob_id(self, content: bytes) -> str:
        """git 给这段内容的 blob id，不经 `hash-object`：它会跟着符号链接去读目标文件。"""
        if self._object_format is None:
            self._object_format = self.run('rev-parse', '--show-object-format').decode().strip()
        digest = (
            hashlib.sha256(usedforsecurity=False)
            if self._object_format == 'sha256'
            else hashlib.sha1(usedforsecurity=False)
        )
        digest.update(b'blob %d\0' % len(content))
        digest.update(content)
        return digest.hexdigest()
