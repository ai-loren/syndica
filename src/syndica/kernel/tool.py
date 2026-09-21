"""工具基类与 schema 生成。

给模型看的与给代码用的必须分开（`output` / `meta`）：混在一起只能二选一——要么模型上下文被
结构化数据污染，要么工作流只能解析给人看的文本。

schema 三级回退 `__tool_schema__` → `__tool_model__` → 反射：工具描述质量直接决定模型是否用
对它，因此必须允许手写调优；多数工具不需要那么精细，反射足够。三级之间只按「声明了没有」分派，
`None` 是未声明的唯一写法；反射遇到读不懂的标注一律报错，不静默降级——降级会让模型收到一份缺
参数的 schema，然后调用失败得莫名其妙。

**一个工具有三条互相独立的身份轴：注册表的键、`name`、`schema['name']`。** 三者必须逐字相同，
`validate_tool_names` 是那道守卫，收下工具表的每一处都要过它。不校验的后果不是「某个调用失败」：
模型按 `schema['name']` 发起调用、executor 按键查表，键写错则**每一次**调用都落空，而 run 照常
以 COMPLETED 返回——失败被当成成功上报。
"""

from __future__ import annotations

import copy
import inspect
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, cast, get_args, get_origin

from pydantic import BaseModel

from syndica.kernel.context import ArtifactRef, TrustLevel
from syndica.kernel.errors import ConfigError

__all__ = ['Tool', 'ToolResult', 'tool_name_disagreement', 'validate_tool_names']

_JSON_TYPES: dict[type, str] = {
    str: 'string',
    int: 'integer',
    float: 'number',
    bool: 'boolean',
    list: 'array',
    dict: 'object',
}
_SUPPORTED_ANNOTATIONS: str = ', '.join(sorted(annotation.__name__ for annotation in _JSON_TYPES))
# 三级各自字面构造，键集合却必须一致：`.description` / `.parameters` 是 schema 的投影，手写那一级
# 缺键时得报得出缺了什么。
_SCHEMA_KEYS: tuple[str, ...] = ('name', 'description', 'parameters')
# PEP 563 只字符串化一层，作者手写引号会再套一层，两层足够；不设上限时 `_X = '_X'` 这类自指别名
# 会让求值不收敛。
_MAX_ANNOTATION_EVALS: int = 2


def _resolved(annotation: Any, param_name: str, globalns: dict[str, Any]) -> Any:
    """逐层求值一个标注，直到不是字符串。

    `from __future__ import annotations` 下标注在运行时是字符串，而 `inspect.signature` 不求值
    字符串；作者自己加的引号会再套一层（`text: 'str'` 的标注是 `"'str'"`）。只求值参数标注，返回
    标注不碰；求值作用域是 execute 所在模块的全局，函数局部作用域里定义的类型在这里解析不了。
    """
    resolved = annotation
    for _ in range(_MAX_ANNOTATION_EVALS):
        if not isinstance(resolved, str):
            break
        try:
            resolved = eval(resolved, globalns)
        except Exception as exc:
            raise ConfigError(
                expected=f'参数 {param_name!r} 的标注能在 execute 所在模块的全局作用域里求值',
                received=f'标注 {resolved!r} 求值失败：{type(exc).__name__}: {exc}',
                next_action=(
                    f'把 {param_name!r} 标注用到的 import 移出 if TYPE_CHECKING'
                    '（反射在运行时求值标注，TYPE_CHECKING 块在运行时不执行），'
                    f'或把 {param_name!r} 的标注类型移到模块顶层，'
                    '或给这个工具声明 __tool_model__ / __tool_schema__ 跳过反射'
                ),
            ) from exc
    return resolved


def _json_type(annotation: Any, param_name: str, globalns: dict[str, Any]) -> str:
    resolved = _resolved(annotation, param_name, globalns)
    # `Annotated[int, Field(...)]` 是 pydantic 的惯用写法，元数据不参与 JSON 类型判定。
    if get_origin(resolved) is Annotated:
        resolved = get_args(resolved)[0]
    # 先过 isinstance 再查表：泛型别名与联合类型不是 type，其中一部分还不可哈希，直接查表会撞
    # 一个不提参数名的 TypeError。
    json_type = _JSON_TYPES.get(resolved) if isinstance(resolved, type) else None
    if json_type is None:
        raise ConfigError(
            expected=f'参数 {param_name!r} 的标注是 {_SUPPORTED_ANNOTATIONS} 之一',
            received=(
                f'参数 {param_name!r} 没有类型标注'
                if annotation is inspect.Parameter.empty
                else f'参数 {param_name!r} 的标注是 {annotation!r}'
            ),
            next_action=(
                f'把 {param_name!r} 的标注改成受支持的类型，'
                '或给这个工具声明 __tool_model__ / __tool_schema__ 表达更复杂的入参'
            ),
        )
    return json_type


def _own_doc(obj: object) -> str:
    """对象**自己**的 docstring，去缩进；没写则空串。

    不用 `inspect.getdoc`：它沿 MRO 继承，于是没写 docstring 的工具会把基类的内部散文当成自己
    的描述交给模型。
    """
    return inspect.cleandoc(obj.__doc__ or '')


def _parsed_doc(doc: str, param_names: frozenset[str]) -> tuple[str, dict[str, str]]:
    """docstring → （摘要，逐参数描述）。

    参数描述只认单行 `name: 描述`，且 `name` 必须是真实参数名——否则散文里的冒号会变成一条参数
    描述。按**第一个**冒号切分：描述本身常含冒号（`url: https://example.com`），按最后一个切会把
    描述前半段静默丢掉。续行不拼接。摘要在第一个空行或第一条参数描述处结束。
    """
    summary: list[str] = []
    descriptions: dict[str, str] = {}
    in_summary = True
    for raw_line in doc.splitlines():
        line = raw_line.strip()
        name, separator, text = line.partition(':')
        if separator and text.strip() and name.strip() in param_names:
            descriptions[name.strip()] = text.strip()
            in_summary = False
        elif not line:
            in_summary = False
        elif in_summary:
            summary.append(line)
    return '\n'.join(summary), descriptions


@dataclass
class ToolResult:
    """一次工具调用的结果。

    `output` 进模型上下文，`meta` 给工作流代码读，两者不得合并。字段顺序计入公开 API。
    `trust` 默认可信，从外部取内容的工具必须自己标成 UNTRUSTED——标记会随衍生内容传递
    （spec §12.2）。
    """

    output: str | list[Any]
    meta: dict[str, Any] = field(default_factory=dict[str, Any])
    artifacts: list[ArtifactRef] = field(default_factory=list[ArtifactRef])
    trust: TrustLevel = TrustLevel.TRUSTED


class Tool(ABC):
    """工具基类。

    三个 ClassVar 决定 schema 从哪来，都不声明就走反射。
    """

    __tool_name__: ClassVar[str | None] = None
    __tool_schema__: ClassVar[dict[str, Any] | None] = None
    __tool_model__: ClassVar[type[BaseModel] | None] = None
    # 这个工具的哪些参数会被**执行**（交给 shell、解释器、远端命令通道）。危险判定只看这些参数。
    #
    # `None` 是「没声明」，判定按最坏情况看待——检查全部参数。`()` 是「我什么都不执行」，判定于是
    # 完全跳过它。两者不能合并：写文档的工具若被当成执行工具，一篇讲 `rm -rf` 的文档就会被阻断；
    # 执行工具若因为忘了声明被跳过，那道闸等于不存在。默认取不安全的那一侧。
    #
    # **收文本的工具必须显式声明 `()`。** 未声明那条路会把参数里的散文当命令读——一段 markdown
    # 表格里的 `| sh |`、一个围栏代码块里的 `sudo apt install` 都会命中，于是整个工具被阻断。
    # 声明是一行，而不声明的症状是一次没有出处的拒绝。
    #
    # 名字必须是 `execute` 收得到的参数名，否则那条声明静默落空——校验在 `danger` 的判定入口。
    __tool_command_args__: ClassVar[tuple[str, ...] | None] = None

    @property
    def name(self) -> str:
        """模型看到的名字。

        默认取类名并去掉**全部**前导下划线：下划线是 Python 的私有标记，对模型没有含义，而
        `_Grep` 与 `__Grep` 是同一个工具的两种写法。两侧空白一并去掉：判空判的是 strip 后的值，
        原样交出会让 `'  Grep  '` 进模型请求，而多数 provider 对工具名有字符集约束。剩下空串时
        报错而不是交出一个没有名字的工具。
        """
        declared = self.__tool_name__
        resolved = (declared if declared is not None else type(self).__name__.lstrip('_')).strip()
        if not resolved:
            raise ConfigError(
                expected='工具名非空：模型按名字调用工具，没有名字的工具调不到',
                received=(
                    f'__tool_name__={declared!r}'
                    if declared is not None
                    else f'类名 {type(self).__name__!r} 去掉前导下划线后为空'
                ),
                next_action='给这个类声明 __tool_name__，或换一个去掉前导下划线后仍非空的类名',
            )
        return resolved

    @property
    def description(self) -> str:
        return self._projected('description')

    @property
    def parameters(self) -> dict[str, Any]:
        return self._projected('parameters')

    def _projected(self, key: str) -> Any:
        """schema 的单键投影，缺键时替裸 KeyError 说清缺了什么。

        校验整套 `_SCHEMA_KEYS` 而不只校验 `key`：手写 schema 的键集合已由另两级完全确定，一份
        缺 `parameters` 的 schema 对模型已经不可用，只报被读到的那个键会让作者修一次再撞一次。
        """
        schema = self.schema
        missing = [name for name in _SCHEMA_KEYS if name not in schema]
        if missing:
            raise ConfigError(
                expected=f'工具 schema 含 {", ".join(_SCHEMA_KEYS)} 三个键',
                received=(
                    f'{type(self).__name__} 的 __tool_schema__ 缺 {missing}，'
                    f'实际的键是 {sorted(schema)}'
                ),
                next_action=(
                    '把缺的键补进 __tool_schema__，'
                    '或删掉 __tool_schema__ 改用 __tool_model__ / 反射自动生成'
                ),
            )
        return schema[key]

    @property
    def schema(self) -> dict[str, Any]:
        """独立副本，每次读都重新生成。

        `__tool_schema__` 是 ClassVar：交出它本身，调用方一次就地修改就污染了该工具的所有实例与
        此后每一次读取。
        """
        return copy.deepcopy(self._schema())

    def _schema(self) -> dict[str, Any]:
        if self.__tool_schema__ is not None:
            return self.__tool_schema__
        if self.__tool_model__ is not None:
            return {
                'name': self.name,
                'description': _own_doc(type(self)),
                'parameters': self.__tool_model__.model_json_schema(),
            }
        return self._reflected_schema()

    def _reflected_schema(self) -> dict[str, Any]:
        # 绑定方法：`self` 于是结构上就不在参数里。
        execute = self.execute
        globalns: dict[str, Any] = getattr(execute, '__globals__', {})
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for param_name, param in inspect.signature(execute).parameters.items():
            # 必须先跳过再解析标注：`*args: Any` 的标注本身不受支持，反过来写会让每个带变长
            # 参数的工具直接报错。
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            # 仅位置参数报错而不是跳过：模型只会按 schema 填一个 kwargs dict，这个参数它永远填不
            # 进去。收进 schema 则每次调用都因缺必填参数失败，跳过则是交给模型一份缺参数的
            # schema——两条都只在调用时炸，且炸得莫名其妙。
            if param.kind is inspect.Parameter.POSITIONAL_ONLY:
                raise ConfigError(
                    expected=f'参数 {param_name!r} 能按名字传：模型按 schema 填一个 kwargs dict',
                    received=f'参数 {param_name!r} 是仅位置参数（execute 签名里有 / 分隔符）',
                    next_action=(
                        f'把 {param_name!r} 移到 / 之后，或删掉 execute 签名里的 /，'
                        '或给这个工具声明 __tool_model__ / __tool_schema__ 跳过反射'
                    ),
                )
            properties[param_name] = {'type': _json_type(param.annotation, param_name, globalns)}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        summary, descriptions = _parsed_doc(_own_doc(execute), frozenset(properties))
        for param_name, text in descriptions.items():
            properties[param_name]['description'] = text
        return {
            'name': self.name,
            'description': summary,
            'parameters': {'type': 'object', 'properties': properties, 'required': required},
        }

    @abstractmethod
    async def execute(self, *args: Any, **kwargs: Any) -> ToolResult:
        """执行一次调用。

        这里的签名故意是渐进式的：参数由模型按 schema 填，具体工具必须把它收窄成命名参数，反射
        才有东西可读。重试也不可能成功的错误抛 ToolFatalError。
        """

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        """这次调用能否与别的工具并发执行。

        默认 False，只有确认本次调用不写任何共享状态才该覆写成 True。**只有字面 `True` 算数**：
        真值对象在这里被读成「可以并发」，而这个方向的错不可回收。

        **它坐在每次工具调用的必经之路上**，且会被问到本批里**根本不会执行**的调用——分段在任何
        调用跑起来之前就要算完，那时还不知道谁会因为同批的失败而停下。因此它必须是纯函数：不读
        外部状态、不做 I/O、不抛出、不慢。抛出时执行器判这次调用不安全（只是串行）并记一条
        WARNING，此后这个工具在每一批里都单独跑。

        「不写任何共享状态」说的是**这次调用自己**。它不承诺别人看不见这次调用的产出：同一段里的
        兄弟互相看不到对方落进轨迹的结果，一个 `PRE_TOOL_USE` hook 若从旁路把兄弟的产出展开进参
        数，执行器只能按溯源失败处理（spec §13）。
        """
        return False


def tool_name_disagreement(key: str, tool: Tool) -> ConfigError | None:
    """三条身份轴（注册表键 / `name` / `schema['name']`）不一致时给出错误，一致则 None。"""
    name = tool.name
    schema = tool.schema
    schema_name: object = schema.get('name')
    if key == name and schema_name == name:
        return None
    cls = type(tool).__name__
    if 'name' not in schema:
        fix = f"把 'name' 补进 {cls}.__tool_schema__（值写 {key!r}），或删掉手写 schema 改用反射"
    elif schema_name != name:
        fix = (
            f'把 {cls}.__tool_schema__["name"] 改成 {name!r}，'
            f'或给 {cls} 声明 __tool_name__ = {schema_name!r}'
        )
    else:
        fix = f'把 dict 键改成 {name!r}，或给 {cls} 声明 __tool_name__ = {key!r}'
    return ConfigError(
        expected='工具的三条身份轴逐字相同: 注册表键 == tool.name == schema["name"]',
        received=(
            f'{cls} 上三者不一致: 注册表键 {key!r}、'
            f'tool.name {name!r}、schema["name"] {schema_name!r}'
        ),
        next_action=(
            f'{fix}；模型按 schema["name"] 发起调用、executor 按注册表键查表，'
            '两者不同则每一次调用都必然落空，而 run 仍以 COMPLETED 返回'
        ),
    )


def validate_tool_names(tools: object) -> None:
    """校验整张工具表的身份轴。收下工具表的每一处都必须调它。

    参数标成 `object`：要挡的正是违反 Mapping 标注的运行时取值。声明式清单里
    `tools: [Read, Grep]` 是名字列表，直接丢进来会撞裸 AttributeError，三件套零条。
    """
    if isinstance(tools, (str, bytes)) or not isinstance(tools, Mapping):
        raise ConfigError(
            expected=(
                'tools 是「工具名 -> Tool」的映射：'
                '模型按 schema["name"] 发起调用、executor 按键查表'
            ),
            received=f'收到 {type(tools).__name__} 类型的 {tools!r}，它没有 items()',
            next_action=(
                '把工具表改成 {名字: Tool} 的 dict。'
                '声明式清单里 `tools: [Read, Grep]` 是名字列表，没有 Tool 实例；'
                '先经 resolve_tool_names 把名字解析成映射，再交给这里'
            ),
        )
    table = cast(Mapping[object, object], tools)
    for key, tool in table.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(
                expected='工具表的键是非空白工具名 str',
                received=f'键 {key!r}，类型是 {type(key).__name__}',
                next_action='把键改成与 tool.name / schema["name"] 逐字相同的非空白字符串',
            )
        if not isinstance(tool, Tool):
            raise ConfigError(
                expected=f'工具表键 {key!r} 的取值是 Tool 实例',
                received=f'收到 {tool!r}，类型是 {type(tool).__name__}',
                next_action='传入 Tool 子类的实例，不要把工具名字符串放进映射的值',
            )
        error = tool_name_disagreement(key, tool)
        if error is not None:
            raise error
