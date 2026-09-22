import time
from dataclasses import fields

import pytest

from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode


def _info() -> AgentInfo:
    return AgentInfo(agent_id='a1', name='Worker', agent_type='declarative')


def test_six_states_exist():
    """取值撞车会让成员静默塌成别名（六态塌成五态）；取值随 trace 落盘，属对外契约。"""
    assert {state.name: state.value for state in AgentStatus} == {
        'IDLE': 'idle',
        'RUNNING': 'running',
        'PAUSED': 'paused',
        'WAITING': 'waiting',
        'ABORTING': 'aborting',
        'ZOMBIE': 'zombie',
    }


def test_three_exit_modes_exist():
    """取值撞车会让成员静默塌成别名；取值随 trace 落盘，属对外契约。"""
    assert {mode.name: mode.value for mode in ExitMode} == {
        'IDLE': 'idle',
        'ZOMBIE': 'zombie',
        'EXIT': 'exit',
    }


def test_agent_states_and_exit_modes_do_not_equal_their_serialized_strings():
    """枚举与落盘字符串必须显式转换；混入 str 会掩盖漏写 `.value` 的调用方错误。"""
    assert AgentStatus.IDLE != 'idle'
    assert AgentStatus.ZOMBIE != 'zombie'
    assert ExitMode.IDLE != 'idle'
    assert ExitMode.EXIT != 'exit'


def test_info_defaults_to_idle_with_no_children():
    info = _info()
    assert info.status is AgentStatus.IDLE
    assert info.children == []
    assert info.parent_id is None


def test_info_defaults_to_no_session_and_empty_metadata():
    info = _info()
    assert info.session_id is None
    assert info.metadata == {}
    assert info.flags == {}


def test_info_starts_with_no_lease_holder():
    """回收逻辑靠 owner 为空区分「无人认领」与「持有者心跳过期」。"""
    info = _info()
    assert info.owner is None
    assert info.heartbeat_at is None


def test_info_carries_lease_fields():
    """租约用于回收进程崩溃后留下的孤儿状态（spec §4.3）。"""
    info = AgentInfo(
        agent_id='a1',
        name='Worker',
        agent_type='declarative',
        owner='proc-1',
        heartbeat_at=123.0,
    )
    assert info.owner == 'proc-1'
    assert info.heartbeat_at == 123.0


def test_created_at_is_stamped_at_construction_and_is_not_the_lease_heartbeat():
    """heartbeat_at 是租约活性；回收策略要的年龄是 created_at。"""
    before = time.time()
    info = _info()
    after = time.time()
    assert before <= info.created_at <= after
    assert info.heartbeat_at is None


def test_default_containers_are_not_shared_between_infos():
    """默认值若被共享，一个 Agent 的子代与元数据会出现在另一个 Agent 上。"""
    first = _info()
    second = _info()
    first.children.append('child-1')
    first.metadata['origin'] = 'first'
    first.flags['mode'] = 'first'
    assert second.children == []
    assert second.metadata == {}
    assert second.flags == {}


def test_info_requires_agent_id_name_and_agent_type():
    """agent_id 是 Registry 主键，三者任一悄悄变成可选都会让条目共用缺省值而相撞。"""
    with pytest.raises(TypeError):
        AgentInfo(name='Worker', agent_type='declarative')  # pyright: ignore[reportCallIssue]
    with pytest.raises(TypeError):
        AgentInfo(agent_id='a1', agent_type='declarative')  # pyright: ignore[reportCallIssue]
    with pytest.raises(TypeError):
        AgentInfo(agent_id='a1', name='Worker')  # pyright: ignore[reportCallIssue]


def test_agent_info_field_order_is_public_and_flags_are_appended_last():
    assert [field.name for field in fields(AgentInfo)] == [
        'agent_id',
        'name',
        'agent_type',
        'status',
        'parent_id',
        'children',
        'session_id',
        'metadata',
        'owner',
        'heartbeat_at',
        'created_at',
        'flags',
    ]
