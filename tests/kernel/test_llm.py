import copy
import dataclasses

import pytest

from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, LLMResponseError, SyndicaError
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


def test_config_rejects_client_args_that_are_not_a_mapping():
    with pytest.raises(ConfigError) as excinfo:
        LLMConfig(provider='scripted', model='m1', client_args=42)  # pyright: ignore[reportArgumentType]
    assert 'client_args' in excinfo.value.expected
    assert '42' in excinfo.value.received
    assert excinfo.value.next_action


def test_config_rejects_request_args_that_are_not_a_mapping():
    with pytest.raises(ConfigError) as excinfo:
        LLMConfig(provider='scripted', model='m1', request_args=42)  # pyright: ignore[reportArgumentType]
    assert 'request_args' in excinfo.value.expected
    assert excinfo.value.next_action


def test_config_accepts_mapping_client_and_request_args():
    cfg = LLMConfig(
        provider='scripted',
        model='m1',
        client_args={'base_url': 'http://host'},
        request_args={'temperature': 0},
    )
    assert cfg.client_args == {'base_url': 'http://host'}
    assert cfg.request_args == {'temperature': 0}


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
    """字段清单断言确切内容且**有序**：写死成字面量的校验集合会把新增字段误判成未知字段；
    而字段顺序是位置构造的公开契约，互换声明不该悄悄通过。"""
    assert [f.name for f in dataclasses.fields(LLMConfig)] == [
        'provider',
        'model',
        'client_args',
        'request_args',
        'timeout',
        'context_window',
    ]
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


def test_provider_and_model_are_the_first_two_positional_fields():
    """互换两处字段声明，`LLMConfig('openai', 'gpt-4o')` 会静默把缓存键与计费归属对调——
    正是 derive 要防的那种错位，只不过发生在构造处。"""
    cfg = LLMConfig('openai', 'gpt-4o')
    assert (cfg.provider, cfg.model) == ('openai', 'gpt-4o')


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


def test_copy_copy_shares_the_argument_dicts_while_deepcopy_does_not():
    """`copy.copy` 绕过构造器，「字典由本配置独占」在这条路径上不成立；docstring 记了这个陷阱，
    用例保证它不会悄悄换一种行为，让 docstring 变成错的。"""
    cfg = _config()
    shallow = copy.copy(cfg)
    shallow.client_args['base_url'] = 'http://elsewhere'
    assert cfg.client_args == {'base_url': 'http://elsewhere'}

    deep = copy.deepcopy(cfg)
    deep.client_args['base_url'] = 'http://third'
    assert cfg.client_args == {'base_url': 'http://elsewhere'}


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


def test_derive_accepts_a_field_a_subclass_declares_for_itself():
    """字段清单从 `type(self)` 取。写死母类时子类的 derive 会拒绝它自己声明的字段，
    错误消息还会列出一份不含该字段的清单，把调用方引向错误的结论。"""

    @dataclasses.dataclass(frozen=True)
    class TenantConfig(LLMConfig):
        tenant: str = 'default'

    derived = TenantConfig(provider='scripted', model='m1').derive(tenant='acme', model='m2')
    assert isinstance(derived, TenantConfig)
    assert (derived.tenant, derived.model) == ('acme', 'm2')


def test_context_overflow_error_belongs_to_the_family_and_carries_the_contract():
    err = _overflow()
    assert isinstance(err, SyndicaError)
    assert '压缩上下文后重试' in str(err)


def test_context_overflow_is_not_an_llm_response_error():
    """超窗是请求侧、压缩后可重试；LLMResponseError 是响应不可用。Task 13 的三级分类按这个
    继承关系分支，一旦把它嵌进去，先写的 `except LLMResponseError` 会吞掉超窗，压缩永不触发。"""
    assert issubclass(ContextOverflowError, SyndicaError)
    assert not issubclass(ContextOverflowError, LLMResponseError)


def test_scripted_llm_satisfies_the_llm_protocol():
    llm: LLM = ScriptedLLM([ScriptedTurn(content='ok')])
    assert isinstance(llm, LLM)
    assert isinstance(llm.config, LLMConfig)


def test_scripted_llm_defaults_to_a_synthetic_config_with_an_unknown_window():
    """默认窗口保持 None：「窗口未知」是真实状态，替身编一个数字会让压缩判定测到假前提。"""
    cfg = ScriptedLLM([]).config
    assert (cfg.provider, cfg.model, cfg.context_window) == ('scripted', 'scripted', None)


def test_scripted_llm_exposes_the_config_it_was_given():
    """把窗口设小是驱动「接近窗口就压缩」的唯一确定性手段，替身必须原样交回它拿到的那一份。"""
    cfg = LLMConfig(provider='scripted', model='m1', context_window=100)
    assert ScriptedLLM([], config=cfg).config is cfg


def test_config_cannot_be_reassigned_on_an_llm():
    """只读才逼得包装器去问委托对象。可写就能自己存一份构造时的副本，于是缓存包装器声明
    200k 而内层模型实际 8k——正是这个 property 要消除的那种「两个必须一致的东西」。"""
    llm = ScriptedLLM([])
    other = LLMConfig(provider='other', model='m2')
    with pytest.raises(AttributeError):
        llm.config = other  # pyright: ignore[reportAttributeAccessIssue]


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


async def test_an_all_defaults_turn_yields_an_empty_assistant_block():
    """空响应守卫以 `content is None` 为条件——`ContextBlock.content` 区分「没说话」与「说了空
    字符串」。替身若把 None 归一成 ''，守卫永不触发，而驱动它的用例会因为没报错而显得通过。"""
    llm = ScriptedLLM([ScriptedTurn()])
    block = await llm.create([])
    assert block.role == 'assistant'
    assert block.content is None
    assert block.tool_calls == []
    assert block.tool_call_id is None
    assert block.artifacts == []
    assert block.extra == {}
    assert block.trust is TrustLevel.TRUSTED


def test_a_turn_cannot_both_raise_and_reply():
    """两者都给时必有一个被丢掉。静默的优先级让「我明明给了 content」变成一次难查的失败。"""
    with pytest.raises(ConfigError):
        ScriptedTurn(content='done', raises=_overflow())
    with pytest.raises(ConfigError):
        ScriptedTurn(
            tool_calls=[ToolCall(id='c1', name='Echo', arguments='{}')], raises=_overflow()
        )


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


async def test_mutating_a_sent_block_afterwards_does_not_rewrite_what_was_recorded():
    """记录若存调用方的块本身，事后改一个字段就追溯性地改写了「当时发了什么」。"""
    llm = ScriptedLLM([ScriptedTurn(content='ok')])
    sp = ContextBlock(role='system', content='SP')
    await llm.create([sp])
    sp.content = 'rewritten after the call'
    sp.tool_calls.append(ToolCall(id='c1', name='Echo', arguments='{}'))
    sp.extra['injected'] = True
    assert llm.calls[0][0].content == 'SP'
    assert llm.calls[0][0].tool_calls == []
    assert llm.calls[0][0].extra == {}


async def test_one_block_sent_twice_is_recorded_as_two_independent_snapshots():
    """turn 循环跨轮复用同一个 SP block（`current_traj` 换新 list，块还是那一批）。记录共享对象
    时 `calls[0][0] is calls[1][0]` 恒成立，「前缀逐轮字节一致」这条前缀缓存性质就成了恒等式——
    循环每轮重建出不同前缀也照样绿。逐次独立的快照才让相等断言真的在断相等。"""
    llm = ScriptedLLM([ScriptedTurn(content='a'), ScriptedTurn(content='b')])
    sp = ContextBlock(role='system', content='SP')
    await llm.create([sp])
    await llm.create([sp, ContextBlock(role='user', content='q')])
    assert llm.calls[0][0] is not llm.calls[1][0]
    assert llm.calls[0][0] == llm.calls[1][0]


async def test_per_call_kwargs_are_recorded_alongside_the_messages():
    """循环靠 kwargs 递交 tool schema 与单次覆盖参数；不记就无从断言它到底交了什么下去。"""
    llm = ScriptedLLM([ScriptedTurn(content='ok'), ScriptedTurn(content='ok')])
    await llm.create([], tools=[{'name': 'Echo'}], temperature=0)
    await llm.create([])
    assert llm.call_kwargs == [{'tools': [{'name': 'Echo'}], 'temperature': 0}, {}]
    assert len(llm.call_kwargs) == len(llm.calls)


async def test_one_kwarg_value_sent_twice_is_recorded_as_two_independent_snapshots():
    """turn 循环跨轮递交同一个 tool schema 列表对象。记录共享它时「每轮的工具定义都是这一份」
    只在最终值也偏离时才会红——轮间就地改一下再改回来全程隐形，而那正是前缀缓存失效的形状。"""
    llm = ScriptedLLM([ScriptedTurn(content='a'), ScriptedTurn(content='b')])
    tools = [{'name': 'Echo'}]
    await llm.create([], tools=tools)
    tools[0]['name'] = 'Drifted'
    await llm.create([], tools=tools)
    assert [kwargs['tools'] for kwargs in llm.call_kwargs] == [
        [{'name': 'Echo'}],
        [{'name': 'Drifted'}],
    ]


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
