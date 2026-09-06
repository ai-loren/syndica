"""工具基类与 schema 生成。

给模型看的与给代码用的必须分开（`output` / `meta`）：混在一起只能二选一——要么模型上下文被
结构化数据污染，要么工作流只能解析给人看的文本。

schema 三级回退 `__tool_schema__` → `__tool_model__` → 反射：工具描述质量直接决定模型是否用
对它，因此必须允许手写调优；多数工具不需要那么精细，反射足够。三级之间只按「声明了没有」分派，
`None` 是未声明的唯一写法；反射遇到读不懂的标注一律报错，不静默降级——降级会让模型收到一份缺
参数的 schema，然后调用失败得莫名其妙。
"""

from __future__ import annotations

import copy
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from syndica.kernel.context import ArtifactRef, TrustLevel
from syndica.kernel.errors import ConfigError

__all__ = ['Tool', 'ToolResult']

_JSON_TYPES: dict[type, str] = {
    str: 'string',
    int: 'integer',
    float: 'number',
    bool: 'boolean',
    list: 'array',
    dict: 'object',
}
_SUPPORTED_ANNOTATIONS: str = ', '.join(sorted(annotation.__name__ for annotation in _JSON_TYPES))


def _resolved(annotation: Any, param_name: str, globalns: dict[str, Any]) -> Any:
    """求值一个标注。

    `from __future__ import annotations` 下标注在运行时是字符串，而 `inspect.signature` 不求值
    字符串。只求值参数标注，返回标注不碰；求值作用域是 execute 所在模块的全局，函数局部作用域里
    定义的类型在这里解析不了。
    """
    if not isinstance(annotation, str):
        return annotation
    try:
        return eval(annotation, globalns)
    except Exception as exc:
        raise ConfigError(
            expected=f'参数 {param_name!r} 的标注能在 execute 所在模块的全局作用域里求值',
            received=f'标注 {annotation!r} 求值失败：{type(exc).__name__}: {exc}',
            next_action=(
                f'把 {param_name!r} 的标注类型移到模块顶层，'
                '或给这个工具声明 __tool_model__ / __tool_schema__ 跳过反射'
            ),
        ) from exc


def _json_type(annotation: Any, param_name: str, globalns: dict[str, Any]) -> str:
    resolved = _resolved(annotation, param_name, globalns)
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
    描述。续行不拼接。摘要在第一个空行或第一条参数描述处结束。
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

    @property
    def name(self) -> str:
        """模型看到的名字。

        默认取类名并去掉**全部**前导下划线：下划线是 Python 的私有标记，对模型没有含义，而
        `_Grep` 与 `__Grep` 是同一个工具的两种写法。剩下空串时报错而不是交出一个没有名字的工具。
        """
        declared = self.__tool_name__
        resolved = declared if declared is not None else type(self).__name__.lstrip('_')
        if not resolved.strip():
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
        return self.schema['description']

    @property
    def parameters(self) -> dict[str, Any]:
        return self.schema['parameters']

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

        默认 False，只有确认本次调用不写任何共享状态才该覆写成 True。
        """
        return False
