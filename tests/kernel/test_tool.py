"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行
时是字符串。少了它，测试里的工具类持有真类型对象，反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated, Any

import pytest
from pydantic import BaseModel, Field, Json

from syndica.kernel.context import ArtifactRef, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.tool import (
    Tool,
    ToolResult,
    tool_name_disagreement,
    validate_tool_names,
)

if TYPE_CHECKING:
    from decimal import Decimal


class _Reflected(Tool):
    async def execute(self, text: str, times: int = 1) -> ToolResult:
        """把 text 重复 times 次。

        text: 要重复的内容
        times: 重复次数
        """
        return ToolResult(output=text * times)


class _Grep(Tool):
    """参数名刻意非字典序（`pattern` < `path` 的排序形式相反）：`a, b, c` 那样的取名让「声明序」
    与「字典序」的断言无从区分，顺序漂移测不出来。"""

    async def execute(self, pattern: str, path: str, limit: int = 20) -> ToolResult:
        """在 path 下搜索 pattern。

        pattern: 正则表达式
        path: 搜索根目录
        limit: 最多返回几条
        """
        return ToolResult(output='')


class _ModelArgs(BaseModel):
    path: str
    depth: int = 1


class _WithModel(Tool):
    """按 __tool_model__ 生成 schema。"""

    __tool_model__ = _ModelArgs

    async def execute(self, path: str) -> ToolResult:
        return ToolResult(output=path)


class _BlobArgs(BaseModel):
    blob: Json[dict[str, Any]]


class _WithJsonBlob(Tool):
    """把 blob 落盘。"""

    __tool_model__ = _BlobArgs

    async def execute(self, blob: str) -> ToolResult:
        return ToolResult(output=blob)


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


def test_only_leading_underscores_are_dropped_from_the_class_name():
    class _Grep_(Tool):
        async def execute(self) -> ToolResult:
            return ToolResult(output='')

    assert _Grep_().name == 'Grep_'


def test_a_padded_declared_tool_name_is_stripped():
    """判空判的是 strip 后的值：原样交出会让带空白的名字进模型请求，而多数 provider 对工具名有
    字符集约束。"""

    class _Padded(Tool):
        __tool_name__ = '  Grep  '

        async def execute(self) -> ToolResult:
            return ToolResult(output='')

    assert _Padded().name == 'Grep'
    assert _Padded().schema['name'] == 'Grep'


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
    assert list(_Grep().schema['parameters']['properties']) == ['pattern', 'path', 'limit']
    assert list(_AllTypes().schema['parameters']['properties']) == ['a', 'b', 'c', 'd', 'e', 'f']


def test_the_schema_emits_its_keys_in_a_fixed_order():
    """键集合是 `.description` / `.parameters` 两个投影的依据，键序则进请求前缀——两者都是公开
    契约，而 dict 相等比较对键序是瞎的（spec §5.2）。"""
    assert list(_Reflected().schema) == ['name', 'description', 'parameters']
    assert list(_WithModel().schema) == ['name', 'description', 'parameters']
    assert list(_HandWritten().schema) == ['name', 'description', 'parameters']
    assert list(_Reflected().schema['parameters']) == ['type', 'properties', 'required']


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
    assert _Grep().schema['parameters']['required'] == ['pattern', 'path']
    assert _AllTypes().schema['parameters']['required'] == ['a', 'b', 'c', 'd', 'e', 'f']


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


def test_keyword_only_parameters_reach_the_schema():
    """模型按名字填参，`async def execute(self, *, path: str)` 是写工具最自然的形式；跳过这类
    参数会交给模型一份「本工具不接受任何入参」的 schema。"""

    class _KeywordOnly(Tool):
        async def execute(self, *, pattern: str, limit: int = 20) -> ToolResult:
            return ToolResult(output='')

    assert _KeywordOnly().schema['parameters'] == {
        'type': 'object',
        'properties': {'pattern': {'type': 'string'}, 'limit': {'type': 'integer'}},
        'required': ['pattern'],
    }


def test_a_positional_only_parameter_is_rejected_naming_the_parameter():
    """模型只会按 schema 填一个 kwargs dict，仅位置参数它永远填不进去：收进 schema 则每次调用
    都因缺必填参数失败，跳过则是交出一份缺参数的 schema，两条都只在调用时才炸。"""

    class _PositionalOnly(Tool):
        async def execute(self, path: str, /) -> ToolResult:
            return ToolResult(output=path)

    with pytest.raises(ConfigError) as excinfo:
        _ = _PositionalOnly().schema
    assert 'path' in str(excinfo.value)


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


def test_a_parameter_description_keeps_the_colons_inside_it():
    """按最后一个冒号切分会把描述前半段静默丢掉，而 URL、时间格式这类描述里含冒号很常见。"""

    class _Fetch(Tool):
        async def execute(self, url: str) -> ToolResult:
            """抓取 url。

            url: 目标地址，如 https://example.com:8443/a
            """
            return ToolResult(output=url)

    properties = _Fetch().schema['parameters']['properties']
    assert properties['url']['description'] == '目标地址，如 https://example.com:8443/a'


def test_a_bare_parameter_name_without_text_is_not_a_description():
    """空描述比没有描述更糟：模型收到一个 `'description': ''` 的参数，占着请求前缀的字节却什么
    都没说。"""

    class _Bare(Tool):
        async def execute(self, text: str) -> ToolResult:
            """回显 text。

            text:
            """
            return ToolResult(output=text)

    schema = _Bare().schema
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


def test_the_model_tier_builds_the_whole_schema_from_the_model_and_the_class_docstring():
    """`title` / 私有模型类名进了 parameters，是这一级当前的实测输出；按 provider 裁剪属于
    provider 层，本断言先把交出去的东西钉死。"""
    assert _WithModel().schema == {
        'name': 'WithModel',
        'description': '按 __tool_model__ 生成 schema。',
        'parameters': {
            'properties': {
                'path': {'title': 'Path', 'type': 'string'},
                'depth': {'default': 1, 'title': 'Depth', 'type': 'integer'},
            },
            'required': ['path'],
            'title': '_ModelArgs',
            'type': 'object',
        },
    }


def test_the_model_tier_describes_what_execute_accepts_not_what_it_returns():
    """`model_json_schema()` 默认是校验模式。序列化模式给的是模型**输出**的形状：`Json[...]`
    在那边是 object，而工具只接受 JSON 字符串，模型照那份 schema 填参会被 pydantic 拒掉。"""
    assert _WithJsonBlob().schema['parameters'] == {
        'properties': {
            'blob': {
                'contentMediaType': 'application/json',
                'contentSchema': {'additionalProperties': True, 'type': 'object'},
                'title': 'Blob',
                'type': 'string',
            }
        },
        'required': ['blob'],
        'title': '_BlobArgs',
        'type': 'object',
    }


def test_the_declared_name_reaches_the_model_schema():
    class _Renamed(Tool):
        __tool_name__ = 'Grep'
        __tool_model__ = _ModelArgs

        async def execute(self, path: str) -> ToolResult:
            return ToolResult(output=path)

    assert _Renamed().schema['name'] == 'Grep'


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


def test_a_hand_written_schema_name_is_not_reconciled_with_the_tool_name():
    """两个名字来源不同，本类不做调和：只有注册层知道该以哪个为准。这是公开行为，钉住它才能让
    以后真去调和时表现为一次显式的破坏性变更。"""
    tool = _HandWritten()
    assert (tool.name, tool.schema['name']) == ('HandWritten', 'Custom')


def test_description_and_parameters_come_from_the_schema():
    tool = _Reflected()
    assert tool.description == tool.schema['description']
    assert tool.parameters == tool.schema['parameters']


def test_a_hand_written_schema_missing_a_key_is_rejected_naming_the_key():
    class _NoParameters(Tool):
        __tool_schema__ = {'name': 'Custom', 'description': '手写描述'}

        async def execute(self, text: str) -> ToolResult:
            return ToolResult(output=text)

    with pytest.raises(ConfigError) as excinfo:
        _ = _NoParameters().parameters
    assert 'parameters' in str(excinfo.value)


def test_a_projection_rejects_a_schema_missing_another_key_than_its_own():
    """键集合由另两级完全确定，缺一个键这份 schema 对模型就已经不可用；只报被读到的那个键，
    作者会修一次再撞一次。"""

    class _NoParameters(Tool):
        __tool_schema__ = {'name': 'Custom', 'description': '手写描述'}

        async def execute(self, text: str) -> ToolResult:
            return ToolResult(output=text)

    with pytest.raises(ConfigError):
        _ = _NoParameters().description


def test_an_empty_declared_schema_is_handed_out_as_is_and_cannot_be_projected():
    """空 dict 与「没声明」是两件事：真值判断会让它静默落到下一级，作者于是拿到一份自己没写过
    的 schema。`.schema` 照旧原样交出，只有两个投影在读不到键时出声。"""

    class _Empty(Tool):
        __tool_schema__ = {}

        async def execute(self, text: str) -> ToolResult:
            return ToolResult(output=text)

    assert _Empty().schema == {}
    with pytest.raises(ConfigError):
        _ = _Empty().description
    with pytest.raises(ConfigError):
        _ = _Empty().parameters


def test_reading_the_schema_twice_gives_independent_copies():
    tool = _HandWritten()
    first = tool.schema
    first['description'] = 'mutated'
    first['parameters']['properties']['injected'] = {'type': 'string'}
    assert tool.schema['description'] == '手写描述'
    assert tool.schema['parameters']['properties'] == {}


def test_mutating_a_schema_cannot_reach_another_instance_of_the_same_tool():
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


def test_an_annotated_parameter_uses_the_underlying_type():
    """`Annotated[int, Field(...)]` 是 pydantic 的惯用写法，元数据不参与 JSON 类型判定；拒掉它
    等于拒掉一个合法工具。"""

    class _Documented(Tool):
        async def execute(self, limit: Annotated[int, Field(description='最多几条')]) -> ToolResult:
            return ToolResult(output='')

    assert _Documented().schema['parameters']['properties'] == {'limit': {'type': 'integer'}}


def test_an_unhashable_annotation_is_rejected_naming_the_parameter():
    """联合类型的 hash 连带 hash 各分支，`Annotated[int, {...}] | None` 于是不可哈希。不先判
    type 就查表，撞的是一个不提参数名的 TypeError。"""

    class _Bad(Tool):
        async def execute(self, thing: Annotated[int, {'unit': 'ms'}] | None) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError) as excinfo:
        _ = _Bad().schema
    assert 'thing' in str(excinfo.value)


def test_a_quoted_forward_reference_is_resolved():
    """PEP 563 已经把标注字符串化，作者再手写引号就是两层。判成「标注不受支持」会拒掉一个本来
    解析得出 str 的工具，而消息读作「标注是 "'str'"」，作者看不出问题在哪。

    那对引号就是本例的载荷，而 UP037 会把它自动改掉——本仓不写引号，工具作者的仓库不一定。
    """

    class _Quoted(Tool):
        async def execute(self, text: 'str') -> ToolResult:  # noqa: UP037
            return ToolResult(output=text)

    assert _Quoted().schema['parameters']['properties'] == {'text': {'type': 'string'}}


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


def test_a_type_checking_only_annotation_is_rejected_naming_the_parameter():
    """`if TYPE_CHECKING:` 块在运行时不执行，而反射在读 schema 时真的求值标注。这是合法作者最
    容易踩进这条分支的方式，所以 next_action 必须指向「把 import 移出 TYPE_CHECKING」。"""

    class _Deferred(Tool):
        async def execute(self, amount: Decimal) -> ToolResult:
            return ToolResult(output='')

    with pytest.raises(ConfigError) as excinfo:
        _ = _Deferred().schema
    assert 'amount' in str(excinfo.value)


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


class _Greeter(Tool):
    async def execute(self) -> ToolResult:
        """打个招呼。"""
        return ToolResult(output='hi')


class _Renamed(Tool):
    __tool_name__ = 'declared'

    async def execute(self) -> ToolResult:
        """名字来自声明而非类名。"""
        return ToolResult(output='ok')


class _SchemaWithoutName(Tool):
    __tool_schema__ = {'description': 'd', 'parameters': {}}

    async def execute(self) -> ToolResult:
        """手写 schema 漏了 name。"""
        return ToolResult(output='ok')


def test_three_agreeing_axes_are_accepted():
    assert tool_name_disagreement('Greeter', _Greeter()) is None
    assert tool_name_disagreement('declared', _Renamed()) is None
    assert validate_tool_names({'Greeter': _Greeter(), 'declared': _Renamed()}) is None


def test_a_registry_key_that_differs_from_the_tools_name_is_rejected():
    """宿主写 `{'greet': Greeter()}` 时，模型看到的名字是 'Greeter'、executor 查的键是 'greet'，
    于是每一次调用都落空而 run 仍报 COMPLETED——本项目最坏的失败形态。"""
    error = tool_name_disagreement('greet', _Greeter())
    assert isinstance(error, ConfigError)
    assert "'greet'" in error.received
    assert "'Greeter'" in error.received
    assert "'Greeter'" in error.next_action


@pytest.mark.parametrize('key', ['Custom', 'HandWritten'], ids=['keyed-by-schema', 'keyed-by-name'])
def test_a_hand_written_schema_name_that_differs_from_the_tools_name_is_rejected(key):
    """`Tool` 自己刻意不调和这两个名字；调和的判据在收下工具表的那一层。两个键都要拒：与 name 对齐
    的那个键让模型按 schema 里的 'Custom' 调用而 executor 查不到，只校验键与 name 会漏掉它。"""
    error = tool_name_disagreement(key, _HandWritten())
    assert isinstance(error, ConfigError)
    assert "'Custom'" in error.received
    assert "'HandWritten'" in error.received
    assert '__tool_schema__' in error.next_action


def test_a_schema_without_a_name_key_is_rejected():
    error = tool_name_disagreement('SchemaWithoutName', _SchemaWithoutName())
    assert isinstance(error, ConfigError)
    assert '__tool_schema__' in error.next_action


def test_validation_names_the_disagreeing_tool_rather_than_the_first_one():
    """一表多工具时报错必须指向出问题的那个；只看第一项会让后面的错配静默通过。"""
    tools = {'Greeter': _Greeter(), 'wrong': _Renamed(), 'Reflected': _Reflected()}
    with pytest.raises(ConfigError) as excinfo:
        validate_tool_names(tools)
    assert "'wrong'" in excinfo.value.received
    assert "'declared'" in excinfo.value.received


def test_command_arguments_are_undeclared_by_default():
    """默认 None 而不是 ()：没声明必须按最坏情况处理，声明「我什么都不执行」得是显式动作。"""

    class _Plain(Tool):
        async def execute(self, text: str) -> ToolResult:
            """回显 text。"""
            return ToolResult(output=text)

    assert _Plain.__tool_command_args__ is None


def test_a_declared_empty_tuple_is_not_the_same_object_as_the_undeclared_default():
    class _Inert(Tool):
        __tool_command_args__ = ()

        async def execute(self, text: str) -> ToolResult:
            """回显 text。"""
            return ToolResult(output=text)

    assert _Inert.__tool_command_args__ == ()
    assert _Inert.__tool_command_args__ is not None
