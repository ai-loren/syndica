"""会话锁：挡住两个进程同时续 / 跑同一份会话（B-01）。

`SessionStore.save` 是 last-writer-wins（spec §4.4）：两个进程各持同一会话、各自存盘，后写盖掉先写、
整段工作都丢。所以每个给某会话根存盘的 Runtime 都在 `.syndica/sessions/<sid>.lock` 上持一把排他
**`flock`**，持到会话收尾；第二个进程拿不到就报三件套拒绝。

用 `flock(fd)` 而非 pid 文件：锁随 fd 存在，进程崩了内核自动放——无陈旧判定、无 pid 复用 / 命名空间
问题、不往锁文件写内容、`release` 只关 fd 不删文件。

sid 先过 `_valid_sid`：锁路径由 sid 拼成，不校验 `../` 能删 / 动 store 外文件（评审 H1）；锁文件再以
`O_NOFOLLOW` 打开，符号链接重定向不了。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from syndica.kernel.errors import ConfigError
from syndica.kernel.stores.file_session import _valid_sid  # pyright: ignore[reportPrivateUsage]

__all__ = ['SessionLock', 'acquire_session_lock']


class SessionLock:
    """一把持有中的会话锁。`release` 幂等：关 fd 即由内核释放 flock，不删锁文件。"""

    __slots__ = ('_fd',)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def release(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


def acquire_session_lock(sessions_root: Path, sid: str) -> SessionLock:
    """在 `<sessions_root>/<sid>.lock` 上加排他 `flock`。被别的进程持着就报三件套（不等待）。"""
    _valid_sid(sid)
    sessions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(sessions_root / f'{sid}.lock', flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise ConfigError(
            expected=f'会话 {sid} 没有被别的进程续着 / 跑着',
            received='另一个进程正持有这个会话的锁',
            next_action='等那个进程退出；它崩了内核会自动放锁，之后直接重试',
        ) from None
    except OSError:
        os.close(descriptor)
        raise
    return SessionLock(descriptor)
