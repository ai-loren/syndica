import dataclasses

import pytest

from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, SyndicaError
from syndica.kernel.llm import LLM, ContextOverflowError, LLMConfig, ScriptedLLM, ScriptedTurn


def _config() -> LLMConfig:
    return LLMConfig(
        provider='scripted',
        model='m1',
        client_args={'base_url': 'http://host'},
        request_args={'temperature': 0},
    )


def _overflow() -> ContextOverflowError:
    return ContextOverflowError(
        expected='请求在上下文窗口内',
        received='超出窗口',
        next_action='压缩上下文后重试',
    )


def test_config_is_frozen():
    cfg = LLMConfig(provider='scripted', model='m1')
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.model = 'm2'  # pyright: ignore[reportAttributeAccessIssue]


def test_config_requires_provider_and_model():
    """两者决定缓存键与计费归属；任一悄悄变成可选，就会有配置共用缺省身份。"""
    with pytest.raises(TypeError):
        LLMConfig(model='m1')  # pyright: ignore[reportCallIssue]
    with pytest.raises(TypeError):
        LLMConfig(provider='scripted')  # pyright: ignore[reportCallIssue]


def test_config_leaves_every_optional_field_unset_by_default():
    cfg = LLMConfig(provider='scripted', model='m1')
    assert (cfg.client_args, cfg.request_args, cfg.timeout, cfg.context_window) == (
        {},
        {},
        600.0,
        None,
    )


def test_default_argument_dicts_are_not_shared_between_configs():
    first = LLMConfig(provider='scripted', model='m1')
    second = LLMConfig(provider='scripted', model='m1')
    first.client_args['base_url'] = 'http://host'
    first.request_args['temperature'] = 0
    assert second.client_args == {}
    assert second.request_args == {}


def test_derive_returns_a_new_config_and_leaves_the_original_alone():
    cfg = LLMConfig(provider='scripted', model='m1', request_args={'temperature': 0})
    derived = cfg.derive(model='m2')
    assert derived is not cfg
    assert derived.model == 'm2'
    assert derived.request_args == {'temperature': 0}
    assert cfg.model == 'm1'


def test_derive_carries_over_every_field_it_was_not_asked_to_change():
    cfg = LLMConfig(
        provider='scripted',
        model='m1',
        client_args={'base_url': 'http://host'},
        request_args={'temperature': 0},
        timeout=1.5,
        context_window=8192,
    )
    derived = cfg.derive(model='m2')
    assert (
        derived.provider,
        derived.client_args,
        derived.request_args,
        derived.timeout,
        derived.context_window,
    ) == ('scripted', {'base_url': 'http://host'}, {'temperature': 0}, 1.5, 8192)


def test_derive_accepts_every_field_the_config_declares():
    """字段清单断言确切内容：校验用的集合写死成字面量时，新增字段会被 derive 误判成未知字段。"""
    assert {f.name for f in dataclasses.fields(LLMConfig)} == {
        'provider',
        'model',
        'client_args',
        'request_args',
        'timeout',
        'context_window',
    }
    derived = LLMConfig(provider='p1', model='m1').derive(
        provider='p2',
        model='m2',
        client_args={'base_url': 'http://host'},
        request_args={'temperature': 1},
        timeout=1.5,
        context_window=4096,
    )
    assert (
        derived.provider,
        derived.model,
        derived.client_args,
        derived.request_args,
        derived.timeout,
        derived.context_window,
    ) == ('p2', 'm2', {'base_url': 'http://host'}, {'temperature': 1}, 1.5, 4096)


def test_a_config_owns_the_argument_dicts_it_was_constructed_with():
    client_args = {'base_url': 'http://host'}
    request_args = {'temperature': 0}
    cfg = LLMConfig(
        provider='scripted',
        model='m1',
        client_args=client_args,
        request_args=request_args,
    )
    client_args['base_url'] = 'http://elsewhere'
    request_args['temperature'] = 1
    assert cfg.client_args == {'base_url': 'http://host'}
    assert cfg.request_args == {'temperature': 0}


def test_mutating_a_derived_configs_args_cannot_reach_its_parent():
    cfg = _config()
    derived = cfg.derive(model='m2')
    derived.client_args['base_url'] = 'http://elsewhere'
    derived.request_args['temperature'] = 1
    assert cfg.client_args == {'base_url': 'http://host'}
    assert cfg.request_args == {'temperature': 0}


def test_mutating_a_parent_configs_args_cannot_reach_a_config_derived_from_it():
    cfg = _config()
    derived = cfg.derive(model='m2')
    cfg.client_args['base_url'] = 'http://elsewhere'
    cfg.request_args['temperature'] = 1
    assert derived.client_args == {'base_url': 'http://host'}
    assert derived.request_args == {'temperature': 0}


def test_derive_rejects_a_field_the_config_does_not_have():
    """`dataclasses.replace` 的裸 TypeError 既不列出合法字段也不说下一步。"""
    cfg = LLMConfig(provider='scripted', model='m1')
    with pytest.raises(ConfigError) as excinfo:
        cfg.derive(mdoel='m2')
    assert 'mdoel' in excinfo.value.received
    assert 'model' in excinfo.value.expected
    assert 'context_window' in excinfo.value.expected


def test_derive_rejecting_one_unknown_field_names_all_of_them():
    cfg = LLMConfig(provider='scripted', model='m1')
    with pytest.raises(ConfigError) as excinfo:
        cfg.derive(mdoel='m2', temperature=0)
    assert 'mdoel' in excinfo.value.received
    assert 'temperature' in excinfo.value.received


def test_context_overflow_error_belongs_to_the_family_and_carries_the_contract():
    err = _overflow()
    assert isinstance(err, SyndicaError)
    assert '压缩上下文后重试' in str(err)


def test_scripted_llm_satisfies_the_llm_protocol():
    llm: LLM = ScriptedLLM([ScriptedTurn(content='ok')])
    assert isinstance(llm, LLM)


async def test_scripted_llm_returns_turns_in_order():
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]),
            ScriptedTurn(content='done'),
        ]
    )
    first = await llm.create([ContextBlock(role='user', content='go')])
    assert first.tool_calls[0].name == 'Echo'
    second = await llm.create([ContextBlock(role='user', content='go')])
    assert second.content == 'done'
    assert second.tool_calls == []


async def test_a_turn_comes_back_as_a_trusted_assistant_block():
    """模型自己的回复是可信的：替身若把它标成不可信，靠它驱动的不可信传播测试全都在测错前提。"""
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    block = await llm.create([])
    assert block.role == 'assistant'
    assert block.content == 'done'
    assert block.trust is TrustLevel.TRUSTED


async def test_scripted_llm_can_raise_to_test_failure_paths():
    """失败路径无法要求真模型在指定的那一轮触发，这是脚本化假 LLM 存在的核心理由。"""
    boom = _overflow()
    llm = ScriptedLLM([ScriptedTurn(raises=boom)])
    with pytest.raises(ContextOverflowError) as excinfo:
        await llm.create([ContextBlock(role='user', content='go')])
    assert excinfo.value is boom


async def test_a_raising_turn_is_consumed_rather_than_replayed():
    """索引若在抛出之前不前进，同一轮无限重放，被测循环的失败处理分支永远走不到下一轮。"""
    llm = ScriptedLLM([ScriptedTurn(raises=_overflow()), ScriptedTurn(content='after')])
    with pytest.raises(ContextOverflowError):
        await llm.create([])
    assert (await llm.create([])).content == 'after'


async def test_scripted_llm_exhaustion_is_a_clear_error():
    llm = ScriptedLLM([ScriptedTurn(content='only one')])
    await llm.create([])
    with pytest.raises(ConfigError):
        await llm.create([])


async def test_a_call_beyond_the_script_is_still_recorded():
    """被测循环多跑一轮时，`calls` 是唯一能看出它到底调了几次的地方。"""
    llm = ScriptedLLM([])
    with pytest.raises(ConfigError):
        await llm.create([ContextBlock(role='user', content='go')])
    assert [[block.role for block in call] for call in llm.calls] == [['user']]


async def test_scripted_llm_records_received_messages():
    llm = ScriptedLLM([ScriptedTurn(content='ok'), ScriptedTurn(content='ok')])
    await llm.create(
        [ContextBlock(role='system', content='SP'), ContextBlock(role='user', content='q')]
    )
    await llm.create([ContextBlock(role='user', content='again')])
    assert [[block.role for block in call] for call in llm.calls] == [
        ['system', 'user'],
        ['user'],
    ]


async def test_recorded_messages_are_a_snapshot_of_the_callers_list():
    llm = ScriptedLLM([ScriptedTurn(content='ok')])
    messages = [ContextBlock(role='user', content='go')]
    await llm.create(messages)
    messages.append(ContextBlock(role='user', content='appended after the call'))
    messages.clear()
    assert [block.content for block in llm.calls[0]] == ['go']


async def test_returned_tool_calls_are_not_shared_with_the_script():
    """脚本会跨轮、跨用例复用；消费者改动返回的块不得改到脚本本身。"""
    turn = ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{}')])
    llm = ScriptedLLM([turn])
    block = await llm.create([])
    block.tool_calls[0].name = 'Rewritten'
    block.tool_calls.clear()
    assert [(call.id, call.name) for call in turn.tool_calls] == [('c1', 'Echo')]


async def test_the_script_is_snapshotted_at_construction():
    script = [ScriptedTurn(content='first')]
    llm = ScriptedLLM(script)
    script.append(ScriptedTurn(content='appended after construction'))
    assert (await llm.create([])).content == 'first'
    with pytest.raises(ConfigError):
        await llm.create([])
