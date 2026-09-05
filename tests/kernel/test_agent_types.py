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


def test_info_defaults_to_idle_with_no_children():
    info = _info()
    assert info.status is AgentStatus.IDLE
    assert info.children == []
    assert info.parent_id is None


def test_info_defaults_to_no_session_and_empty_metadata():
    info = _info()
    assert info.session_id is None
    assert info.metadata == {}


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


def test_default_containers_are_not_shared_between_infos():
    """默认值若被共享，一个 Agent 的子代与元数据会出现在另一个 Agent 上。"""
    first = _info()
    second = _info()
    first.children.append('child-1')
    first.metadata['origin'] = 'first'
    assert second.children == []
    assert second.metadata == {}
