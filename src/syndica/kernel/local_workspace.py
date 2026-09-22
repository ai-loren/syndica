"""本地目录工作区。内核里唯一读写这块磁盘的实现。

路径是否在根内，只看解析后的真实路径能否 `relative_to` 根。字符串前缀会把
`/root-evil` 和指向根外的符号链接都放进来。
不读进程环境，不查当前工作目录：根、shell、env 都是构造参数。
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import signal
import stat
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.workspace import ExecResult, Portal

__all__ = ['LocalWorkspace']

# 超过这个大小的文件拒绝整篇读入。模型上下文装不下，拒绝时必须报出实际字节数。
_MAX_READ_BYTES: int = 1024 * 1024


@dataclass
class _ProcessRecord:
    process: asyncio.subprocess.Process
    communicating: asyncio.Task[tuple[bytes, bytes]]


class LocalWorkspace:
    """根目录、shell、子进程环境都由宿主传入。"""

    def __init__(self, root: str, *, shell: str, env: Mapping[str, str]) -> None:
        self._root = _existing_absolute_dir(root)
        self._shell = _executable(shell)
        self._env = _copied_env(env)
        self._processes: dict[str, _ProcessRecord] = {}
        self._process_lock = asyncio.Lock()
        self._closing: asyncio.Task[None] | None = None
        self._closed = False

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
        size = real.stat().st_size
        if size > _MAX_READ_BYTES:
            raise ConfigError(
                expected=f'单文件不超过 {_MAX_READ_BYTES} 字节',
                received=f'文件有 {size} 字节',
                next_action='换一个不超过 1 MiB 的文件',
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
        if '..' in Path(pattern).parts:
            raise ConfigError(
                expected='pattern 是不含 ..、不会离开工作区根目录的相对模式',
                received=f'pattern={pattern!r} 含父目录段 ..',
                next_action='去掉 ..，从工作区根目录开始写相对模式',
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
        self._ensure_open()
        seconds = _positive_timeout(timeout)
        record = await self._spawn_process(command, cwd)
        try:
            async with asyncio.timeout(seconds):
                stdout, stderr = await asyncio.shield(record.communicating)
        except TimeoutError as exc:
            await _kill_process(record)
            raise ConfigError(
                expected=f'命令在 {seconds:g} 秒内完成',
                received=f'命令超时：运行超过 {seconds:g} 秒，已终止整个进程组',
                next_action='缩小命令范围、改为后台运行，或传入更大的正数 timeout',
            ) from exc
        except BaseException:
            await _kill_process(record)
            raise
        return ExecResult(
            exit_code=record.process.returncode,
            stdout=_decoded(stdout),
            stderr=_decoded(stderr),
            truncated=False,
            running=False,
        )

    async def start(self, command: str, *, cwd: str) -> str:
        async with self._process_lock:
            self._ensure_open()
            record = await self._spawn_process(command, cwd)
            handle = uuid.uuid4().hex
            self._processes[handle] = record
            return handle

    async def poll(self, handle: str) -> ExecResult:
        self._ensure_open()
        record = self._record(handle)
        if not record.communicating.done():
            return ExecResult(
                exit_code=None,
                stdout='',
                stderr='',
                truncated=False,
                running=True,
            )
        stdout, stderr = await record.communicating
        return ExecResult(
            exit_code=record.process.returncode,
            stdout=_decoded(stdout),
            stderr=_decoded(stderr),
            truncated=False,
            running=False,
        )

    async def kill(self, handle: str) -> None:
        self._ensure_open()
        await _kill_process(self._record(handle))

    async def close(self) -> None:
        if self._closed:
            return
        async with self._process_lock:
            if self._closed:
                return
            if self._closing is None:
                self._closing = asyncio.create_task(self._close_processes())
            closing = self._closing
        await asyncio.shield(closing)

    async def _spawn_process(self, command: str, cwd: str) -> _ProcessRecord:
        working_directory = self._directory(cwd)
        process = await asyncio.create_subprocess_exec(
            self._shell,
            '-c',
            command,
            cwd=str(working_directory),
            env=dict(self._env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return _ProcessRecord(
            process=process,
            communicating=asyncio.create_task(process.communicate()),
        )

    async def _close_processes(self) -> None:
        try:
            for record in tuple(self._processes.values()):
                await _kill_process(record)
        finally:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed or self._closing is not None:
            raise ConfigError(
                expected='工作区仍然打开，可以执行或查询命令',
                received='工作区已关闭或正在关闭',
                next_action='新建一个 LocalWorkspace；close() 之后同一个工作区不再接收进程操作',
            )

    def _record(self, handle: str) -> _ProcessRecord:
        record = self._processes.get(handle)
        if record is None:
            raise ConfigError(
                expected='handle 是本工作区 start() 返回的后台命令句柄',
                received=f'收到未知 handle={handle!r}',
                next_action='使用 Bash 启动后台命令后返回的 handle，不要复用其他工作区的句柄',
            )
        return record

    def _inside(self, path: str) -> Path:
        real = self._resolved(path)
        self._must_stay(real)
        return real

    def _directory(self, path: str) -> Path:
        real = self._inside(path)
        if not real.is_dir():
            raise ConfigError(
                expected='cwd 指向工作区根目录内的目录',
                received=f'cwd={path!r} 不是目录',
                next_action='传入工作区根目录内的相对目录，或用 "." 表示根目录',
            )
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


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(
            expected='timeout 是有限正数秒数',
            received=f'timeout={value!r}，类型是 {type(value).__name__}',
            next_action='传入大于 0 的 int 或 float，不要传布尔值',
        )
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ConfigError(
            expected='timeout 是有限正数秒数',
            received=f'timeout={value!r}',
            next_action='传入大于 0 的有限秒数；0、负数、inf 与 nan 都不合法',
        )
    return seconds


def _decoded(value: bytes) -> str:
    return value.decode('utf-8', errors='replace')


async def _kill_process(record: _ProcessRecord) -> None:
    if record.process.returncode is None:
        try:
            os.killpg(record.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.shield(record.communicating)
    except asyncio.CancelledError:
        if not record.communicating.done():
            record.communicating.cancel()
        raise


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
