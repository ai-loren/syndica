"""宿主事件循环的默认执行器：阻塞调用跑在守护线程里，关循环与进程退出都不等它。

asyncio 把阻塞调用（httpx 解析 collector 的域名、`to_thread`）放进默认执行器。超时只是不再等它，
线程照样跑：标准库的线程池在关循环时要 join 它（3.11 不设上限），进程退出时还要再 join 一次，DNS 慢
的时候结论早已打出、进程却迟迟不退，按 Ctrl-C 也没用。超时之后没人要它的结果，所以不等它。

`workers` 给了就是固定数目的守护线程，多出来的调用排队：长驻的 server 里每个模型请求都经它解析
一次域名，一个调用一个线程就没有上界。一次性的 CLI 不给，一个调用一个线程。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, ParamSpec, TypeVar

__all__ = ['DaemonExecutor']

_P = ParamSpec('_P')
_T = TypeVar('_T')

_Job = tuple['Future[Any]', Callable[..., Any], tuple[Any, ...], dict[str, Any]]


class DaemonExecutor(ThreadPoolExecutor):
    """`set_default_executor` 只收 `ThreadPoolExecutor`；池子本身不用：池里的线程在进程退出时会被
    join。"""

    def __init__(self, workers: int | None = None) -> None:
        super().__init__(max_workers=1)
        self._jobs: queue.SimpleQueue[_Job] | None = None
        if workers is not None:
            if isinstance(workers, bool) or workers < 1:
                raise ValueError(f'workers 是 >= 1 的整数或 None；收到 {workers!r}')
            jobs: queue.SimpleQueue[_Job] = queue.SimpleQueue()
            self._jobs = jobs
            for index in range(workers):
                threading.Thread(
                    target=_serve_jobs, args=(jobs,), name=f'syndica-blocking-{index}', daemon=True
                ).start()

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        future: Future[_T] = Future()
        if self._jobs is not None:
            self._jobs.put((future, fn, args, kwargs))
            return future
        threading.Thread(
            target=_run, args=(future, fn, args, kwargs), name='syndica-blocking', daemon=True
        ).start()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        del wait, cancel_futures


def _serve_jobs(jobs: queue.SimpleQueue[_Job]) -> None:
    while True:
        _run(*jobs.get())


def _run(
    future: Future[Any], fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    if not future.set_running_or_notify_cancel():
        return
    try:
        result = fn(*args, **kwargs)
    except BaseException as error:
        future.set_exception(error)
    else:
        future.set_result(result)
