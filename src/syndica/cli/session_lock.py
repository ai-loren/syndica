"""恢复锁：挡住两个进程同时续同一份会话（B-01）。

`SessionStore.save` 是 last-writer-wins（spec §4.4），不支持并发恢复同一个会话——两个进程各自续它、
各自存盘，后写的盖掉前写的，两段工作都丢。所以恢复前在 `.syndica/sessions/<sid>.lock` 上加一把锁：
`O_EXCL` 建、写本进程 pid。锁已存在时读 pid 判断持有者还在不在——活着就拒（另一个进程在续它），死了
（陈旧，进程崩了没清理）就接管。锁一直持到会话收尾，`close` 时解。

pid 不是密钥，写进锁文件与 `kernel/versions.py` 往它的锁文件写 pid 同理，在 `test_write_points` 的
`_ALLOWED` 里按文件写明。
"""

from __future__ import annotations

import os
from pathlib import Path

from syndica.kernel import ConfigError

__all__ = ['ResumeLock', 'acquire_resume_lock']


class ResumeLock:
    """一把持有中的恢复锁。`release` 幂等：删不掉（已被删、目录没了）也不抛。"""

    __slots__ = ('_path',)

    def __init__(self, path: Path) -> None:
        self._path = path

    def release(self) -> None:
        try:
            os.unlink(self._path)
        except OSError:
            pass


def acquire_resume_lock(sessions_root: Path, sid: str) -> ResumeLock:
    """在 `<sessions_root>/<sid>.lock` 上加锁，拿不到（别的活进程持着）报三件套。"""
    sessions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = sessions_root / f'{sid}.lock'
    # 两次：第一次撞上已存在的锁、而它是陈旧的（持有者崩了），删掉再试一次；第二次仍撞上说明有活
    # 进程刚接管，拒绝。
    for first_try in (True, False):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if first_try and _stale(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            raise ConfigError(
                expected=f'会话 {sid} 没有被别的进程续着',
                received=f'{path.name} 已存在，且持有它的进程还活着',
                next_action='等那个进程退出再续；它崩了就删掉这个 .lock 文件再重试',
            ) from None
        try:
            os.write(descriptor, str(os.getpid()).encode('ascii'))
        finally:
            os.close(descriptor)
        return ResumeLock(path)
    raise AssertionError('acquire_resume_lock 的重试循环没有返回也没有抛出')


def _stale(path: Path) -> bool:
    """锁文件的持有者是不是已经没了。读不出 pid、pid 非法、或那个进程不在，都算陈旧。"""
    try:
        pid = int(path.read_text(encoding='ascii').strip())
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # 进程在，只是不归本用户——不是陈旧。
        return False
    return False
