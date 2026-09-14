import dataclasses

import pytest

from syndica.kernel.context import (
    ArtifactRef,
    Context,
    ContextBlock,
    StopReason,
    ToolCall,
    TrustLevel,
    provenance_of,
)


def _ref(version: str = 'v1') -> ArtifactRef:
    return ArtifactRef(
        id='a1',
        path='report.md',
        kind='document',
        mime='text/markdown',
        version=version,
        producer='agent-1',
    )


def test_append_preserves_order():
    ctx = Context()
    ctx.append(ContextBlock(role='user', content='hi'))
    ctx.append(ContextBlock(role='assistant', content='hello'))
    assert [b.role for b in ctx.current_traj] == ['user', 'assistant']


def test_block_defaults_to_trusted():
    assert ContextBlock(role='user', content='hi').trust is TrustLevel.TRUSTED


def test_untrusted_marking_is_explicit():
    """取值撞车会让成员静默塌成别名（UNTRUSTED 即 TRUSTED）；取值随 trace 落盘，属对外契约。"""
    block = ContextBlock(role='tool', content='<html>', trust=TrustLevel.UNTRUSTED)
    assert block.trust is TrustLevel.UNTRUSTED
    assert {level.name: level.value for level in TrustLevel} == {
        'TRUSTED': 'trusted',
        'UNTRUSTED': 'untrusted',
    }


def test_block_carries_artifact_refs():
    ref = ArtifactRef(
        id='a1',
        path='report.md',
        kind='document',
        mime='text/markdown',
        version='v1',
        producer='agent-1',
        is_deliverable=True,
    )
    block = ContextBlock(role='tool', content='written', artifacts=[ref])
    assert block.artifacts[0].id == 'a1'


def test_tool_call_keeps_raw_arguments():
    """参数解析失败也必须留下原文，否则无法诊断模型输出了什么。"""
    call = ToolCall(id='c1', name='Read', arguments='{not json')
    assert call.arguments == '{not json'


def test_stop_reason_starts_unset():
    ctx = Context()
    assert ctx.stop_reason is None
    ctx.stop_reason = StopReason.COMPLETED
    assert ctx.stop_reason is StopReason.COMPLETED


def test_current_traj_returns_a_copy_so_callers_cannot_rewrite_history():
    ctx = Context()
    ctx.append(ContextBlock(role='user', content='hi'))
    ctx.current_traj.append(ContextBlock(role='user', content='forged'))
    ctx.current_traj.clear()
    assert [b.content for b in ctx.current_traj] == ['hi']


def test_default_containers_are_not_shared_between_blocks():
    """默认值若被共享，一个块的工具调用与产物会出现在另一个块上。"""
    first = ContextBlock(role='assistant', content='a')
    second = ContextBlock(role='assistant', content='b')
    first.tool_calls.append(ToolCall(id='c1', name='Read', arguments='{}'))
    first.artifacts.append(_ref())
    first.extra['origin'] = 'first'
    assert second.tool_calls == []
    assert second.artifacts == []
    assert second.extra == {}


def test_artifact_ref_rejects_mutation():
    ref = _ref()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.path = 'moved.md'  # pyright: ignore[reportAttributeAccessIssue]


def test_artifact_refs_are_hashable_by_value():
    """ArtifactStore 按值索引版本，因此值相等必须同时意味着 == 与 hash 相等。"""
    assert _ref() == _ref()
    assert len({_ref(), _ref()}) == 1
    assert len({_ref(), _ref(version='v2')}) == 2


def test_artifacts_are_intermediate_until_declared_deliverable():
    """隐式追踪会收进临时脚本与调试输出；默认交付会让验收去检查没人关心的东西（spec §7.4）。"""
    assert _ref().is_deliverable is False


def test_stop_reason_covers_every_terminal_outcome():
    """取值撞车会让成员静默塌成别名（四态塌成三态）；取值随 trace 落盘，属对外契约。"""
    assert {reason.name: reason.value for reason in StopReason} == {
        'COMPLETED': 'completed',
        'ABORTED': 'aborted',
        'ERROR': 'error',
        'MAX_TURNS': 'max_turns',
    }


def test_provenance_is_untrusted_when_any_block_anywhere_in_the_trajectory_is():
    """它是一个**上界**：位置与 role 都不参与判断，一块就够（spec §12.2 第 3 条那道升级的判据）。"""
    ctx = Context()
    ctx.append(ContextBlock(role='user', content='clean'))
    ctx.append(
        ContextBlock(role='tool', content='<html>', tool_call_id='c1', trust=TrustLevel.UNTRUSTED)
    )
    ctx.append(ContextBlock(role='assistant', content='conclusion'))

    assert provenance_of(ctx) is TrustLevel.UNTRUSTED


def test_provenance_of_a_clean_trajectory_is_trusted_and_never_none():
    """看完整份轨迹之后报「没溯源过」就是把做过的检查说成没做过，那道升级于是恒不命中。"""
    ctx = Context()
    ctx.append(ContextBlock(role='user', content='clean'))

    assert provenance_of(ctx) is TrustLevel.TRUSTED
    assert provenance_of(Context()) is TrustLevel.TRUSTED
