"""宿主事件循环的默认执行器：每个阻塞调用一个守护线程，关循环与进程退出都不等它。

asyncio 把阻塞调用（httpx 解析 collector 的域名、`to_thread`）放进默认执行器。超时只是不再等它，
线程照样跑：标准库的线程池在关循环时要 join 它（3.11 不设上限），进程退出时还要再 join 一次，DNS 慢
的时候结论早已打出、进程却迟迟不退，按 Ctrl-C 也没用。超时之后没人要它的结果，所以不等它。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import ParamSpec, TypeVar

__all__ = ['DaemonExecutor']

_P = ParamSpec('_P')
_T = TypeVar('_T')


class DaemonExecutor(ThreadPoolExecutor):
    """`set_default_executor` 只收 `ThreadPoolExecutor`；池子本身不用：池里的线程在进程退出时会被
    join。"""

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        future: Future[_T] = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        threading.Thread(target=run, name='syndica-blocking', daemon=True).start()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        del wait, cancel_futures
