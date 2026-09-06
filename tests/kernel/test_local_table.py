import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.local_table import LocalAgentTable


class _FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name


async def _noop() -> None:
    return None


async def _blocked() -> None:
    await asyncio.Event().wait()


@pytest.fixture
async def running() -> AsyncIterator[Callable[[], asyncio.Task[None]]]:
    """交出不会自行结束的任务；用例退出前必须取消，否则事件循环关闭时会报 Task was destroyed。"""
    tasks: list[asyncio.Task[None]] = []

    def make() -> asyncio.Task[None]:
        task = asyncio.create_task(_blocked())
        tasks.append(task)
        return task

    yield make
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


def test_put_then_get_returns_same_object():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    agent = _FakeAgent('w1')
    table.put('a1', agent)
    assert table.get('a1') is agent


def test_get_unknown_returns_none():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    assert table.get('missing') is None


def test_get_returns_the_agent_stored_under_the_requested_id():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    first = _FakeAgent('w1')
    second = _FakeAgent('w2')
    table.put('a1', first)
    table.put('a2', second)
    assert table.get('a1') is first
    assert table.get('a2') is second


async def test_task_is_stored_alongside_agent():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    task = asyncio.create_task(_noop())
    table.set_task('a1', task)
    assert table.get_task('a1') is task
    await task


async def test_get_task_returns_the_task_stored_under_the_requested_id():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    table.put('a2', _FakeAgent('w2'))
    first = asyncio.create_task(_noop())
    second = asyncio.create_task(_noop())
    table.set_task('a1', first)
    table.set_task('a2', second)
    assert table.get_task('a1') is first
    assert table.get_task('a2') is second
    await first
    await second


async def test_put_accepts_the_task_alongside_the_agent():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    task = asyncio.create_task(_noop())
    table.put('a1', _FakeAgent('w1'), task)
    assert table.get_task('a1') is task
    await task


def test_get_task_is_none_when_only_the_agent_was_stored():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    assert table.get_task('a1') is None


async def test_storing_a_task_leaves_the_agent_in_place():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    agent = _FakeAgent('w1')
    table.put('a1', agent)
    task = asyncio.create_task(_noop())
    table.set_task('a1', task)
    assert table.get('a1') is agent
    assert table.get_task('a1') is task
    await task


def test_put_rejects_an_agent_id_that_is_already_present():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    first = _FakeAgent('w1')
    table.put('a1', first)
    with pytest.raises(ConfigError) as excinfo:
        table.put('a1', _FakeAgent('w2'))
    assert table.get('a1') is first
    assert 'a1' in str(excinfo.value)
    assert 'pop' in excinfo.value.next_action


def test_put_rejects_a_duplicate_even_when_the_agent_is_the_same_object():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    agent = _FakeAgent('w1')
    table.put('a1', agent)
    with pytest.raises(ConfigError):
        table.put('a1', agent)
    assert table.get('a1') is agent


async def test_rejected_put_leaves_the_stored_pair_untouched():
    """覆写会让 slot 里的实例与任务描述两个不同的 Agent，而幸存的任务再没有句柄可取消。"""
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    agent = _FakeAgent('w1')
    task = asyncio.create_task(_noop())
    table.put('a1', agent, task)
    with pytest.raises(ConfigError):
        table.put('a1', _FakeAgent('w2'))
    assert table.get('a1') is agent
    assert table.get_task('a1') is task
    await task


async def test_set_task_rejects_an_agent_id_with_no_agent():
    """agent_ids() 只列 Agent，落在它之外的任务 teardown 永远取不到，只能泄漏。"""
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    task = asyncio.create_task(_noop())
    with pytest.raises(ConfigError) as excinfo:
        table.set_task('a1', task)
    assert table.get_task('a1') is None
    assert table.agent_ids() == []
    assert 'put' in excinfo.value.next_action
    await task


async def test_set_task_rejects_replacing_a_task_that_is_still_running(
    running: Callable[[], asyncio.Task[None]],
):
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    first = running()
    table.set_task('a1', first)
    with pytest.raises(ConfigError):
        table.set_task('a1', running())
    assert table.get_task('a1') is first


async def test_set_task_replaces_a_finished_task_so_an_agent_can_be_re_driven():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    first = asyncio.create_task(_noop())
    table.set_task('a1', first)
    await first
    second = asyncio.create_task(_noop())
    table.set_task('a1', second)
    assert table.get_task('a1') is second
    await second


def test_pop_removes_entry():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    table.pop('a1')
    assert table.get('a1') is None
    assert table.agent_ids() == []


async def test_pop_drops_the_task_with_the_agent():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    task = asyncio.create_task(_noop())
    table.put('a1', _FakeAgent('w1'), task)
    table.pop('a1')
    assert table.get('a1') is None
    assert table.get_task('a1') is None
    await task


def test_pop_unknown_id_is_a_noop():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    agent = _FakeAgent('w1')
    table.put('a1', agent)
    table.pop('missing')
    assert table.get('a1') is agent
    assert table.agent_ids() == ['a1']


async def test_pop_accepts_a_task_that_is_still_running(
    running: Callable[[], asyncio.Task[None]],
):
    """EXIT 模式下 Agent 在自己的任务里摘掉自己，拒绝未结束的任务会把那条路堵死。"""
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    task = running()
    table.set_task('a1', task)
    table.pop('a1')
    assert table.get_task('a1') is None
    assert not task.done()


def test_agent_ids_lists_every_stored_agent():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    table.put('a2', _FakeAgent('w2'))
    assert sorted(table.agent_ids()) == ['a1', 'a2']


def test_agent_ids_returns_a_snapshot_the_caller_may_mutate():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    ids = table.agent_ids()
    ids.append('spoofed')
    ids.remove('a1')
    assert table.agent_ids() == ['a1']


def test_agent_ids_survives_popping_every_entry_during_iteration():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    table.put('a2', _FakeAgent('w2'))
    for agent_id in table.agent_ids():
        table.pop(agent_id)
    assert table.get('a1') is None
    assert table.get('a2') is None
    assert table.agent_ids() == []
