"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行
时是字符串。少了它，测试里的工具类持有真类型对象，反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from syndica.kernel.context import ArtifactRef, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.tool import Tool, ToolResult


class _Reflected(Tool):
    async def execute(self, text: str, times: int = 1) -> ToolResult:
        """把 text 重复 times 次。

        text: 要重复的内容
        times: 重复次数
        """
        return ToolResult(output=text * times)


class _ModelArgs(BaseModel):
    path: str
    depth: int = 1


class _WithModel(Tool):
    """按 __tool_model__ 生成 schema。"""

    __tool_model__ = _ModelArgs

    async def execute(self, path: str) -> ToolResult:
        return ToolResult(output=path)


class _HandWritten(Tool):
    """这段 docstring 不该出现在 schema 里。"""

    __tool_schema__ = {
        'name': 'Custom',
        'description': '手写描述',
        'parameters': {'type': 'object', 'properties': {}},
    }
    __tool_model__ = _ModelArgs

    async def execute(self, path: str) -> ToolResult:
        return ToolResult(output='ok')


class _AllTypes(Tool):
    async def execute(self, a: str, b: int, c: float, d: bool, e: list, f: dict) -> ToolResult:
        return ToolResult(output='ok')


class _WithVarArgs(Tool):
    async def execute(self, text: str, *args: Any, **kwargs: Any) -> ToolResult:
        return ToolResult(output=text)


_Path = str


class _Aliased(Tool):
    async def execute(self, path: _Path) -> ToolResult:
        return ToolResult(output=path)


def test_name_defaults_to_the_class_name_without_leading_underscores():
    assert _Reflected().name == 'Reflected'


def test_declared_tool_name_wins_over_the_class_name():
    class _Renamed(Tool):
        __tool_name__ = 'Grep'

        async def execute(self, text: str) -> ToolResult:
            return ToolResult(output=text)

    assert _Renamed().name == 'Grep'


def test_a_class_name_that_is_all_underscores_is_rejected():
    class __(Tool):
        async def execute(self) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError):
        _ = __().name


def test_a_declared_blank_tool_name_is_rejected():
    """`None` 是「未声明」的唯一写法：空串按未声明处理时，作者以为自己设了名字，
    模型看到的却是类名。"""

    class _Empty(Tool):
        __tool_name__ = ''

        async def execute(self) -> ToolResult:
            return ToolResult(output='')

    class _Spaces(Tool):
        __tool_name__ = '  '

        async def execute(self) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError):
        _ = _Empty().name
    with pytest.raises(ConfigError):
        _ = _Spaces().name


def test_reflection_reads_string_annotations():
    """PEP 563 下 `inspect.signature` 交回的是标注字符串。不求值它，每个真实工具都会被判成
    「标注不受支持」。"""
    properties = _Reflected().schema['parameters']['properties']
    assert properties['text']['type'] == 'string'
    assert properties['times']['type'] == 'integer'


def test_annotations_are_resolved_in_the_module_that_defines_execute():
    """受支持的类型全是内建名，`eval` 会自动补 builtins——求值作用域整个丢掉也照样过。模块级
    别名是唯一能看见这件事的地方，也是真实工具里常见的写法。"""
    assert _Aliased().schema['parameters']['properties'] == {'path': {'type': 'string'}}


def test_reflection_builds_the_whole_schema_from_signature_and_docstring():
    assert _Reflected().schema == {
        'name': 'Reflected',
        'description': '把 text 重复 times 次。',
        'parameters': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'description': '要重复的内容'},
                'times': {'type': 'integer', 'description': '重复次数'},
            },
            'required': ['text'],
        },
    }


def test_properties_follow_declaration_order():
    """dict 相等比较不看顺序，而 schema 要被序列化进请求前缀：顺序漂移一次，该会话此后的前缀
    缓存全失效（spec §5.2）。"""
    assert list(_AllTypes().schema['parameters']['properties']) == ['a', 'b', 'c', 'd', 'e', 'f']


def test_reflection_maps_every_supported_annotation_to_its_json_type():
    assert _AllTypes().schema['parameters']['properties'] == {
        'a': {'type': 'string'},
        'b': {'type': 'integer'},
        'c': {'type': 'number'},
        'd': {'type': 'boolean'},
        'e': {'type': 'array'},
        'f': {'type': 'object'},
    }


def test_required_lists_exactly_the_parameters_without_defaults_in_declaration_order():
    assert _AllTypes().schema['parameters']['required'] == ['a', 'b', 'c', 'd', 'e', 'f']
    assert _Reflected().schema['parameters']['required'] == ['text']


def test_self_never_reaches_the_schema():
    assert 'self' not in _Reflected().schema['parameters']['properties']
    assert 'self' not in _Reflected().schema['parameters']['required']


def test_var_args_and_var_kwargs_never_reach_the_schema():
    """它们没有对应的 JSON 属性，且 `Any` 不是受支持的标注——漏掉跳过会让工具直接报错。"""
    assert _WithVarArgs().schema['parameters'] == {
        'type': 'object',
        'properties': {'text': {'type': 'string'}},
        'required': ['text'],
    }


def test_a_tool_without_parameters_gets_an_empty_property_map():
    class _NoParams(Tool):
        """列出当前目录。"""

        async def execute(self) -> ToolResult:
            return ToolResult(output='')

    assert _NoParams().schema['parameters'] == {
        'type': 'object',
        'properties': {},
        'required': [],
    }


def test_the_declared_name_reaches_the_reflected_schema():
    class _Renamed(Tool):
        __tool_name__ = 'Grep'

        async def execute(self, text: str) -> ToolResult:
            return ToolResult(output=text)

    assert _Renamed().schema['name'] == 'Grep'


def test_the_summary_stops_at_the_first_parameter_description():
    """摘要按段落切分时，紧跟摘要的参数描述会被当成摘要的一部分交给模型。"""

    class _Tight(Tool):
        async def execute(self, text: str) -> ToolResult:
            """回显 text。
            text: 要回显的内容
            """
            return ToolResult(output=text)

    schema = _Tight().schema
    assert schema['description'] == '回显 text。'
    assert schema['parameters']['properties']['text']['description'] == '要回显的内容'


def test_a_multi_line_summary_keeps_all_of_its_lines():
    class _Wordy(Tool):
        async def execute(self, text: str) -> ToolResult:
            """第一行摘要。
            第二行摘要。

            text: 要回显的内容
            """
            return ToolResult(output=text)

    assert _Wordy().schema['description'] == '第一行摘要。\n第二行摘要。'


def test_a_prose_line_with_a_colon_is_not_taken_as_a_parameter_description():
    """只有前缀恰好是参数名的行才算参数描述，否则散文里的冒号会造出一条不存在的参数。"""

    class _Prose(Tool):
        async def execute(self, text: str) -> ToolResult:
            """回显 text。

            注意: 这句是散文。
            """
            return ToolResult(output=text)

    schema = _Prose().schema
    assert schema['parameters']['properties'] == {'text': {'type': 'string'}}
    assert schema['description'] == '回显 text。'


def test_parameter_descriptions_are_read_from_an_indented_args_block():
    class _GoogleStyle(Tool):
        async def execute(self, text: str) -> ToolResult:
            """回显 text。

            Args:
                text: 要回显的内容
            """
            return ToolResult(output=text)

    schema = _GoogleStyle().schema
    assert schema['parameters']['properties']['text']['description'] == '要回显的内容'
    assert schema['description'] == '回显 text。'


def test_an_undocumented_parameter_gets_no_description_key():
    class _Partial(Tool):
        async def execute(self, text: str, times: int = 1) -> ToolResult:
            """重复 text。

            text: 要重复的内容
            """
            return ToolResult(output=text * times)

    assert _Partial().schema['parameters']['properties']['times'] == {'type': 'integer'}


def test_a_tool_without_a_docstring_does_not_borrow_the_base_class_one():
    """描述必须来自工具自己的 docstring：沿 MRO 继承会把 harness 的内部散文当成工具描述交给模型。"""

    class _Undocumented(Tool):
        async def execute(self, text: str) -> ToolResult:
            return ToolResult(output=text)

    assert _Undocumented().schema['description'] == ''


def test_the_model_tier_takes_priority_over_reflection():
    """`depth` 只在模型里，不在 execute 签名里；它在不在，就是走了哪一级的证据。"""
    schema = _WithModel().schema
    assert set(schema['parameters']['properties']) == {'path', 'depth'}
    assert schema['name'] == 'WithModel'
    assert schema['description'] == '按 __tool_model__ 生成 schema。'


def test_a_model_tools_description_is_dedented():
    """模型层的描述直接取整份 class docstring，没有逐行处理；不去缩进，源码缩进就跟着进了模型
    上下文，也跟着进了前缀缓存的字节。"""

    class _MultiLine(Tool):
        """第一行摘要。

        第二行仍属于描述。
        """

        __tool_model__ = _ModelArgs

        async def execute(self, path: str) -> ToolResult:
            return ToolResult(output=path)

    assert _MultiLine().schema['description'] == '第一行摘要。\n\n第二行仍属于描述。'


def test_a_model_tool_without_a_class_docstring_does_not_borrow_the_base_class_one():
    class _UndocumentedModel(Tool):
        __tool_model__ = _ModelArgs

        async def execute(self, path: str) -> ToolResult:
            return ToolResult(output=path)

    assert _UndocumentedModel().schema['description'] == ''


def test_the_hand_written_tier_takes_priority_over_the_other_two():
    assert _HandWritten().schema == {
        'name': 'Custom',
        'description': '手写描述',
        'parameters': {'type': 'object', 'properties': {}},
    }


def test_a_declared_schema_is_used_as_is_even_when_it_is_empty():
    """空 dict 与「没声明」是两件事：真值判断会让它静默落到下一级，
    作者于是拿到一份自己没写过的 schema。"""

    class _Empty(Tool):
        __tool_schema__ = {}

        async def execute(self, text: str) -> ToolResult:
            return ToolResult(output=text)

    assert _Empty().schema == {}


def test_description_and_parameters_come_from_the_schema():
    tool = _Reflected()
    assert tool.description == tool.schema['description']
    assert tool.parameters == tool.schema['parameters']


def test_reading_the_schema_twice_gives_independent_copies():
    tool = _HandWritten()
    first = tool.schema
    first['description'] = 'mutated'
    first['parameters']['properties']['injected'] = {'type': 'string'}
    assert tool.schema['description'] == '手写描述'
    assert tool.schema['parameters']['properties'] == {}


def test_mutating_a_schema_cannot_reach_another_instance_of_the_same_tool():
    """`__tool_schema__` 是 ClassVar：交出它本身，一次调用方的改动就污染了该工具的所有实例。"""
    _HandWritten().schema['parameters']['properties']['injected'] = {'type': 'string'}
    assert _HandWritten().schema['parameters']['properties'] == {}


def test_mutating_the_parameters_view_cannot_reach_the_tool():
    tool = _HandWritten()
    tool.parameters['properties']['injected'] = {'type': 'string'}
    assert tool.parameters['properties'] == {}


def test_mutating_a_reflected_schema_cannot_reach_the_next_read():
    tool = _Reflected()
    tool.schema['parameters']['properties']['text']['type'] = 'integer'
    assert tool.schema['parameters']['properties']['text']['type'] == 'string'


def test_mutating_a_model_schema_cannot_reach_the_next_read():
    tool = _WithModel()
    tool.schema['parameters']['properties'].clear()
    assert set(tool.schema['parameters']['properties']) == {'path', 'depth'}


def test_an_unsupported_annotation_is_rejected_naming_the_parameter():
    class _Bad(Tool):
        async def execute(self, thing: object) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError) as excinfo:
        _ = _Bad().schema
    assert 'thing' in str(excinfo.value)


def test_an_unhashable_annotation_is_rejected_naming_the_parameter():
    """`Annotated[int, {...}]` 不可哈希（pydantic 的 `Annotated[int, Field(...)]` 是同一形状）。
    不先判 type 就查表，撞的是一个不提参数名的 TypeError。"""

    class _Bad(Tool):
        async def execute(self, thing: Annotated[int, {'unit': 'ms'}]) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError) as excinfo:
        _ = _Bad().schema
    assert 'thing' in str(excinfo.value)


def test_a_parameter_without_an_annotation_is_rejected_naming_the_parameter():
    class _Unannotated(Tool):
        async def execute(self, thing) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError) as excinfo:
        _ = _Unannotated().schema
    assert 'thing' in str(excinfo.value)


def test_an_unresolvable_annotation_is_rejected_naming_the_parameter():
    """标注在定义它的模块全局里求值，函数局部作用域里的类型解析不了。让 NameError 逃出去，
    作者拿到的是一条从 typing 深处抛出的、不提参数名的栈。"""

    class _LocalArgs:
        pass

    class _Bad(Tool):
        async def execute(self, thing: _LocalArgs) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError) as excinfo:
        _ = _Bad().schema
    assert 'thing' in str(excinfo.value)


def test_an_unresolvable_return_annotation_does_not_block_schema_generation():
    """schema 只需要参数标注。连带求值返回标注，会让返回类型定义在局部作用域的工具无谓地失败。"""

    class _LocalResult(ToolResult):
        pass

    class _Local(Tool):
        async def execute(self, text: str) -> _LocalResult:
            return _LocalResult(output=text)

    assert _Local().schema['parameters']['properties'] == {'text': {'type': 'string'}}


def test_a_subclass_without_execute_cannot_be_instantiated():
    class _NoExecute(Tool):
        pass

    with pytest.raises(TypeError):
        _NoExecute()  # pyright: ignore[reportAbstractUsage]


def test_concurrency_safe_defaults_to_false():
    """代价不对称：并发执行两个改同一文件的工具后果不可逆，串行只是慢。"""
    assert _Reflected().is_concurrency_safe({}) is False


async def test_execute_returns_the_result_the_tool_built():
    result = await _Reflected().execute('ab', times=2)
    assert result.output == 'abab'


def test_result_separates_model_output_from_workflow_meta():
    result = ToolResult(output='for model', meta={'rows': 3})
    assert result.output == 'for model'
    assert result.meta['rows'] == 3


def test_result_defaults_to_trusted_with_no_meta_or_artifacts():
    result = ToolResult(output='x')
    assert (result.meta, result.artifacts, result.trust) == ({}, [], TrustLevel.TRUSTED)


def test_result_requires_an_output():
    """默认成空串会让「工具什么都没返回」与「工具返回了空」无从区分。"""
    with pytest.raises(TypeError):
        ToolResult()  # pyright: ignore[reportCallIssue]


def test_result_fields_are_the_positional_contract():
    assert [f.name for f in dataclasses.fields(ToolResult)] == [
        'output',
        'meta',
        'artifacts',
        'trust',
    ]


def test_default_containers_are_not_shared_between_results():
    first = ToolResult(output='a')
    second = ToolResult(output='b')
    first.meta['rows'] = 3
    first.artifacts.append(
        ArtifactRef(
            id='a1',
            path='out.md',
            kind='doc',
            mime='text/markdown',
            version='v1',
            producer='writer',
        )
    )
    assert second.meta == {}
    assert second.artifacts == []


def test_result_carries_untrusted_content_and_artifacts():
    artifact = ArtifactRef(
        id='a1',
        path='out.md',
        kind='doc',
        mime='text/markdown',
        version='v1',
        producer='writer',
    )
    result = ToolResult(output=['fetched'], artifacts=[artifact], trust=TrustLevel.UNTRUSTED)
    assert result.output == ['fetched']
    assert result.artifacts == [artifact]
    assert result.trust is TrustLevel.UNTRUSTED
