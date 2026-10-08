"""宿主的默认执行器：阻塞调用各在一个守护线程里跑，关循环与进程退出都不等它。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from syndica.cli.executor import DaemonExecutor


def test_a_blocking_call_runs_in_a_daemon_thread_that_shutting_down_does_not_wait_for():
    release = threading.Event()
    seen: list[bool] = []

    def blocking() -> str:
        seen.append(threading.current_thread().daemon)
        release.wait(30)
        return 'late'

    executor = DaemonExecutor()
    future = executor.submit(blocking)
    started = time.monotonic()
    executor.shutdown(wait=True)
    waited = time.monotonic() - started
    release.set()

    assert future.result(timeout=5) == 'late'
    assert seen == [True]
    assert waited < 1.0


def test_results_and_errors_come_back_through_the_future():
    executor = DaemonExecutor()

    def broken() -> None:
        raise OSError('resolver gave up')

    assert executor.submit(sum, [1, 2, 3]).result(timeout=5) == 6
    with pytest.raises(OSError, match='resolver gave up'):
        executor.submit(broken).result(timeout=5)


def test_an_event_loop_closes_without_waiting_for_a_lookup_nobody_wants_any_more():
    release = threading.Event()
    loop = asyncio.new_event_loop()
    loop.set_default_executor(DaemonExecutor())

    async def abandoned() -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await loop.run_in_executor(None, release.wait, 30)

    try:
        loop.run_until_complete(abandoned())
        started = time.monotonic()
        loop.run_until_complete(loop.shutdown_default_executor())
        closing = time.monotonic() - started
    finally:
        loop.close()
        release.set()

    assert closing < 1.0


@pytest.mark.parametrize('workers', [0, -1, True, 2.5, '4'])
def test_a_worker_count_that_is_not_a_positive_integer_is_refused_with_what_to_pass(
    workers: object,
):
    with pytest.raises(ValueError, match='正整数') as refused:
        DaemonExecutor(workers=workers)  # type: ignore[arg-type]
    assert repr(workers) in str(refused.value)
