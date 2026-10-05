"""文件 store 共用的守卫 IO（M3 的 FileTraceStore 抽出来、M6 的会话 / 记忆 store 共用）。

Agent 的 Bash 改得到 `.syndica` 下的目录，所以：每一级按名字以 `O_NOFOLLOW` 重开（目录另加
`O_DIRECTORY`），经 `dir_fd` 一级一级往下走，任何一级换成符号链接写入就被拒；文件另加 `O_NONBLOCK`
打开、打开后核对是普通文件（FIFO 不会把宿主挂住），要写的还得只有一个硬链接（硬链接进来的外部文件
不会被写）；读之前按大小设上限。`root` 的上级由宿主核对：内核不知道工作区在哪。

这里只放与存什么无关的原语。每种 store 的布局、脱敏、身份核对各自实现。错误消息里说「这个文件 /
目录」而不是某一种 store 的名字——它被多种 store 共用。
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager

from syndica.kernel.errors import ContractViolation
from syndica.kernel.redaction import RedactedText, write_redacted

__all__ = [
    'DIRECTORY_FLAGS',
    'append_line',
    'corrupt',
    'not_a_directory',
    'not_a_file',
    'open_directory',
    'open_file',
    'opened',
    'over',
    'read_bytes',
    'read_text',
    'sync_directory',
    'too_large',
    'write_new',
]

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def corrupt(where: str, why: str) -> ContractViolation:
    """`where` 的内容被改过、读不回来：落盘目录在 Agent 的 Bash 够得着的地方，这类错误多半是它。"""
    return ContractViolation(
        expected=f'{where} 是这个 store 写下的、没被改过的内容',
        received=why,
        next_action='这个文件已经不可信（多半是 Agent 的 Bash 改的）：删掉它所在的目录，重新来',
    )


def not_a_directory(where: str, error: OSError) -> ContractViolation:
    return ContractViolation(
        expected=f'{where} 是一个真目录',
        received=f'打不开它：{type(error).__name__}: {error}（是符号链接、普通文件，或没有权限）',
        next_action='删掉或挪开它再来：内容不写到符号链接指向的地方',
    )


def open_directory(name: str | os.PathLike[str], parent: int | None, where: str) -> int:
    try:
        return os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise not_a_directory(where, error) from error


@contextmanager
def opened(name: str, parent: int) -> Generator[int, None, None]:
    descriptor = open_directory(name, parent, name)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def not_a_file(name: str, why: str) -> ContractViolation:
    return ContractViolation(
        expected=f'{name} 是这个 store 建的、只有一个硬链接的普通文件',
        received=why,
        next_action='它被人动过，已经不可信：删掉它所在的目录，重新来',
    )


def open_file(name: str, flags: int, directory: int, *, write: bool) -> tuple[int, int]:
    """按名字打开目录里的一个文件，交回描述符与大小。`O_NONBLOCK`：换成 FIFO 读着不会挂住（写着
    报 ENXIO）；打开后核对是普通文件，要写的还得只有一个硬链接。"""
    try:
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    except (FileNotFoundError, FileExistsError):
        raise
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            raise not_a_file(
                name,
                f'打不开它：{type(error).__name__}: {error}（是符号链接、FIFO 或别的类型的文件）',
            ) from error
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise not_a_file(name, '它不是普通文件（FIFO、设备或目录）')
        if write and info.st_nlink != 1:
            raise not_a_file(name, f'它有 {info.st_nlink} 个硬链接，可能指向目录之外的文件')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, info.st_size


def write_new(directory: int, name: str, text: RedactedText) -> None:
    """建一个新文件写进去（0600）；已经存在时报 `FileExistsError`，是符号链接也一样。写失败删掉
    半截文件：留着它，重试会撞上 `O_EXCL`。"""
    descriptor, _ = open_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, directory, write=True)
    try:
        try:
            write_redacted(descriptor, text)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        os.unlink(name, dir_fd=directory)
        raise


def append_line(directory: int, name: str, text: RedactedText) -> None:
    descriptor, _ = open_file(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, directory, write=True)
    try:
        write_redacted(descriptor, text, newline=True)
    finally:
        os.close(descriptor)


def sync_directory(descriptor: int) -> None:
    """目录里新建与改名的条目落到盘上。有的文件系统不收对目录的 fsync（EINVAL）：那里做不到就算。"""
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise


def over(where: str, received: str, limit: int) -> ContractViolation:
    return corrupt(where, f'{received}，超过这种文件的上限 {limit}')


def too_large(where: str, received: str, limit: int) -> ContractViolation:
    """写入不设上限，长跑可以真的长到这么大：这不是「被改过」。"""
    return ContractViolation(
        expected=f'{where} 不超过读回的上限 {limit} 字节',
        received=received,
        next_action='大到读不回来：被人灌大的就删掉它所在的目录；真的是长跑就直接看文件',
    )


def read_bytes(
    directory: int, name: str, where: str, limit: int, *, growing: bool = False
) -> bytes:
    """整个文件读回；比 `limit` 大的不读。`growing` 的文件随 run 变长，超上限不说明被改过。"""
    descriptor, size = open_file(name, os.O_RDONLY, directory, write=False)
    chunks: list[bytes] = []
    try:
        if size > limit:
            raise (too_large if growing else over)(where, f'有 {size} 字节', limit)
        total = 0
        while chunk := os.read(descriptor, 1 << 16):
            total += len(chunk)
            if total > limit:
                raise (too_large if growing else over)(
                    where, f'读的时候它变大了，读到超过 {limit} 字节', limit
                )
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b''.join(chunks)


def read_text(directory: int, name: str, where: str, limit: int, *, growing: bool = False) -> str:
    """整个文件按 UTF-8 读回，上限同 `read_bytes`。"""
    try:
        return read_bytes(directory, name, where, limit, growing=growing).decode('utf-8')
    except UnicodeDecodeError as error:
        raise corrupt(where, f'不是 UTF-8：{error}') from None
