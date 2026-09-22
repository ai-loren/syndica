"""本地目录工作区。内核里唯一读写这块磁盘的实现。

路径是否在根内，只看解析后的真实路径能否 `relative_to` 根。字符串前缀会把
`/root-evil` 和指向根外的符号链接都放进来。
不读进程环境，不查当前工作目录：根、shell、env 都是构造参数。
"""

from __future__ import annotations

import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.workspace import ExecResult, Portal

__all__ = ['LocalWorkspace']


def _not_yet(name: str) -> ContractViolation:
    return ContractViolation(
        expected=f'{name} 由本地工作区执行',
        received=f'{name} 还没有实现',
        next_action='Task 4 之前不要调用',
    )


class LocalWorkspace:
    """根目录、shell、子进程环境都由宿主传入。"""

    def __init__(self, root: str, *, shell: str, env: Mapping[str, str]) -> None:
        self._root = _existing_absolute_dir(root)
        self._shell = _executable(shell)
        self._env = _copied_env(env)

    @property
    def root(self) -> str:
        return str(self._root)

    def portal(self) -> Portal:
        return self

    async def read_text(self, path: str, *, offset: int, limit: int) -> str:
        if offset < 1:
            raise ConfigError(
                expected='offset 是从 1 开始的行号',
                received=f'offset={offset}',
                next_action='传入大于等于 1 的行号',
            )
        if limit < 1:
            raise ConfigError(
                expected='limit 是正整数',
                received=f'limit={limit}',
                next_action='传入大于等于 1 的行数上限',
            )
        real = self._inside(path)
        if not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 不是文件',
                next_action='改用文件路径',
            )
        lines = _read_utf8(real, path).splitlines()
        start = offset - 1
        if start >= len(lines):
            if len(lines) == 0 and offset == 1:
                return ''
            raise ConfigError(
                expected=f'offset 不超过文件行数 {len(lines)}',
                received=f'offset={offset}',
                next_action='把 offset 调到实际行数以内',
            )
        return '\n'.join(lines[start : start + limit])

    async def write_text(self, path: str, content: str) -> None:
        real = self._inside(path)
        if real.exists() and not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 已存在但不是文件',
                next_action='改用文件路径',
            )
        parent = real.parent
        self._must_stay(parent)
        parent.mkdir(parents=True, exist_ok=True)
        real.write_text(content, encoding='utf-8')

    async def edit_text(self, path: str, old: str, new: str) -> None:
        if old == '':
            raise ConfigError(
                expected='old 是文件里出现恰好一次的非空片段',
                received='old 是空串',
                next_action='传入要替换的原文片段',
            )
        real = self._inside(path)
        if not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 不是文件',
                next_action='改用文件路径',
            )
        text = _read_utf8(real, path)
        count = text.count(old)
        if count != 1:
            raise ConfigError(
                expected='old 在文件中出现恰好 1 次',
                received=f'出现了 {count} 次',
                next_action='把 old 收窄到唯一的一段，或先读出文件再改',
            )
        real.write_text(text.replace(old, new, 1), encoding='utf-8')

    async def glob_paths(self, pattern: str) -> tuple[str, ...]:
        if pattern.strip() == '':
            raise ConfigError(
                expected='pattern 是相对根目录的非空模式',
                received='pattern 是空串',
                next_action='传入相对路径模式',
            )
        if Path(pattern).is_absolute():
            raise ConfigError(
                expected='pattern 是相对根目录的模式',
                received=f'pattern={pattern!r} 是绝对路径',
                next_action='去掉根前缀，只传相对模式',
            )
        found: list[str] = []
        for match in self._root.glob(pattern):
            if not self._stays(match):
                continue
            found.append(match.relative_to(self._root).as_posix())
        found.sort()
        return tuple(found)

    async def grep_text(self, pattern: str, path: str) -> tuple[str, ...]:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                expected='pattern 是合法的正则',
                received=str(exc),
                next_action='修正正则后再搜',
            ) from exc
        start = self._inside('.' if path in ('', '.') else path)
        files = _files_under(start, self._root)
        hits: list[str] = []
        for file in files:
            rel = file.relative_to(self._root).as_posix()
            # 搜索遇到非 utf-8 用替换字符：一次坏文件不能让整次搜索失败。
            text = file.read_text(encoding='utf-8', errors='replace')
            for lineno, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    hits.append(f'{rel}:{lineno}:{line}')
        return tuple(hits)

    async def run(self, command: str, *, timeout: float, cwd: str) -> ExecResult:
        del command, timeout, cwd
        raise _not_yet('run')

    async def start(self, command: str, *, cwd: str) -> str:
        del command, cwd
        raise _not_yet('start')

    async def poll(self, handle: str) -> ExecResult:
        del handle
        raise _not_yet('poll')

    async def kill(self, handle: str) -> None:
        del handle
        raise _not_yet('kill')

    async def close(self) -> None:
        # 没有后台进程可停时，关闭必须成功，并且可以重复调用。
        pass

    def _inside(self, path: str) -> Path:
        real = self._resolved(path)
        self._must_stay(real)
        return real

    def _resolved(self, path: str) -> Path:
        raw = Path(path)
        candidate = raw if raw.is_absolute() else self._root / raw
        return candidate.resolve()

    def _must_stay(self, real: Path) -> None:
        try:
            real.relative_to(self._root)
        except ValueError:
            raise ConfigError(
                expected='路径解析后仍在工作区根目录内',
                received=f'解析后的路径落在根目录外（收到 {real.name!r}）',
                next_action='改用根目录内的相对路径',
            ) from None

    def _stays(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self._root)
        except ValueError:
            return False
        return True


def _read_utf8(path: Path, received: str) -> str:
    # Read / Edit 只接受 utf-8。解码失败必须是 ConfigError，不能把 UnicodeDecodeError 抛出去。
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise ConfigError(
            expected='文件是 utf-8 文本',
            received=f'path={received!r} 不是 utf-8',
            next_action='换一个 utf-8 文本文件',
        ) from exc


def _existing_absolute_dir(root: object) -> Path:
    if not isinstance(root, str) or root.strip() == '' or not Path(root).is_absolute():
        raise ConfigError(
            expected='root 是已存在目录的绝对路径',
            received=f'root={root!r}',
            next_action='传入绝对路径',
        )
    real = Path(root).resolve()
    if not real.is_dir():
        raise ConfigError(
            expected='root 是已存在目录的绝对路径',
            received=f'root={root!r} 不是已存在的目录',
            next_action='传入一个已经存在的目录的绝对路径',
        )
    return real


def _executable(shell: object) -> str:
    if not isinstance(shell, str) or shell.strip() == '' or not Path(shell).is_absolute():
        raise ConfigError(
            expected='shell 是可执行文件的绝对路径',
            received=f'shell={shell!r}',
            next_action='传入绝对路径',
        )
    path = Path(shell)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(
            expected='shell 是可执行文件的绝对路径',
            received=f'shell={shell!r} 不存在',
            next_action='传入一个可执行的绝对路径',
        ) from exc
    if not stat.S_ISREG(mode) or mode & 0o111 == 0:
        raise ConfigError(
            expected='shell 是可执行文件的绝对路径',
            received=f'shell={shell!r} 不可执行',
            next_action='传入一个可执行的绝对路径',
        )
    return shell


def _copied_env(env: object) -> dict[str, str]:
    if not isinstance(env, Mapping):
        raise ConfigError(
            expected='env 是 str 到 str 的映射',
            received=f'env 的类型是 {type(env).__name__}',
            next_action='传入 Mapping[str, str]，不要传入进程环境',
        )
    copied: dict[str, str] = {}
    pairs = cast('Iterable[tuple[object, object]]', env.items())
    for key, value in pairs:
        if not isinstance(key, str) or not isinstance(value, str):
            key_type = type(key).__name__
            value_type = type(value).__name__
            raise ConfigError(
                expected='env 的键和值都是 str',
                received=f'键 {key!r} 的类型是 {key_type}，值的类型是 {value_type}',
                next_action='把 env 的键和值都转成 str',
            )
        copied[key] = value
    return copied


def _files_under(start: Path, root: Path) -> tuple[Path, ...]:
    if start.is_symlink():
        start = start.resolve()
    if start.is_file():
        return (start,)
    if not start.is_dir():
        raise ConfigError(
            expected='path 指向根目录内的文件或目录',
            received=f'path 解析到 {start.name!r}，既不是文件也不是目录',
            next_action='改用根目录内的路径',
        )
    files: list[Path] = []
    for match in start.rglob('*'):
        try:
            real = match.resolve()
            real.relative_to(root)
        except ValueError:
            continue
        if real.is_file():
            files.append(real)
    files.sort()
    return tuple(files)
