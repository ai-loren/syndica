"""把远端 MCP 工具包成 Syndica 工具。

名字规整到 `[A-Za-z0-9_-]{1,64}`。`inputSchema` 里的本地 `$ref` 展开；有环、超深、指到文档外或
指丢了的工具不装，记一条 ERROR。每个顶层属性都声明成命令参数，shell 规则才看得见实参里的命令。
结果一律不可信：对方的 `isError` 只是内容，不是可信的失败结论。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import jsonschema

from syndica.kernel import (
    NO_RULE_MATCHED,
    ConfigError,
    DangerJudge,
    DangerRule,
    DangerVerdict,
    RiskLevel,
    Tool,
    ToolResult,
    TrustLevel,
)

__all__ = [
    'MCP_EXTERNAL_TOOL',
    'ExcludedTool',
    'McpJudge',
    'RemoteTool',
    'build_tools',
    'clean_description',
    'mcp_risk',
    'public_name',
    'render_result',
]

logger = logging.getLogger(__name__)

MCP_EXTERNAL_TOOL = 'mcp.external_tool'
_MAX_NAME = 64
_MAX_SCHEMA_DEPTH = 32
_NAME = re.compile(r'[^A-Za-z0-9_-]')
_REJECT = object()

Invoke = Callable[[str, Mapping[str, object]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ExcludedTool:
    """没装上的远端工具。doctor 读这份名单；日志是它的另一份副本。"""

    server: str
    name: str
    reason: str


@dataclass(frozen=True)
class RemoteTool:
    """一次 `tools/list` 里的一条，已经从 SDK 类型里拆出来。"""

    name: str
    description: object
    input_schema: object
    read_only_hint: object = None
    destructive_hint: object = None


class _InlineFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def public_name(server: str, tool: str) -> str:
    """`<server>__<tool>`，非法字符换成 `_`，最长 64。空结果用 `tool`。"""
    cleaned = _NAME.sub('_', f'{server}__{tool}')
    if not cleaned:
        cleaned = 'tool'
    return cleaned[:_MAX_NAME]


def clean_description(text: object) -> str:
    """去掉控制字符再截到 1024。控制字符能在工具列表里伪造换行。"""
    if not isinstance(text, str):
        return ''
    kept = ''.join(ch for ch in text if unicodedata.category(ch) != 'Cc')
    return kept[:1024]


def mcp_risk(
    name: str,
    *,
    high: frozenset[str],
    low: frozenset[str],
    read_only: object,
    destructive: object,
) -> RiskLevel:
    """配置优先于注解。注解只在 `readOnlyHint` 为真且 `destructiveHint` 不为真时给 LOW。"""
    if name in high:
        return RiskLevel.HIGH
    if name in low:
        return RiskLevel.LOW
    if read_only is True and destructive is not True:
        return RiskLevel.LOW
    return RiskLevel.HIGH


def build_tools(
    server: str,
    remotes: Sequence[RemoteTool],
    *,
    high: frozenset[str],
    low: frozenset[str],
    invoke: Invoke,
) -> tuple[dict[str, Tool], tuple[ExcludedTool, ...]]:
    """装上能装的工具。规整后撞名是配置错误，不静默留其中一个。"""
    built: dict[str, Tool] = {}
    origins: dict[str, str] = {}
    excluded: list[ExcludedTool] = []
    for remote in remotes:
        name = cast(object, remote.name)
        if not isinstance(name, str) or not name:
            item = ExcludedTool(server, repr(name), 'empty-name')
            excluded.append(item)
            _log_excluded(item)
            continue
        inlined, reason = _inline(remote.input_schema)
        if inlined is None:
            item = ExcludedTool(server, name, reason)
            excluded.append(item)
            _log_excluded(item)
            continue
        public = public_name(server, name)
        previous = origins.get(public)
        if previous is not None:
            raise ConfigError(
                expected=f'MCP 服务 {server!r} 的工具名规整到 [A-Za-z0-9_-] 后互不相同',
                received=f'{previous!r} 与 {name!r} 都变成了 {public!r}',
                next_action='在这个服务端改其中一个工具的名字，或换一个服务名',
            )
        origins[public] = name
        built[public] = _make_tool(
            public,
            name,
            clean_description(remote.description),
            inlined,
            mcp_risk(
                name,
                high=high,
                low=low,
                read_only=remote.read_only_hint,
                destructive=remote.destructive_hint,
            ),
            invoke,
        )
    return built, tuple(excluded)


def render_result(content: Sequence[object], structured: object, *, is_error: bool) -> ToolResult:
    """文本拼接；图片和二进制换成占位，不把字节交进上下文。`structuredContent` 同时进 meta。"""
    parts: list[str] = []
    for block in content:
        kind = _field(block, 'type')
        if kind == 'text':
            text = _field(block, 'text')
            if isinstance(text, str):
                parts.append(text)
            continue
        if kind == 'image':
            parts.append(f'[image omitted mime={_mime(block)}]')
            continue
        if kind == 'audio':
            parts.append(f'[audio omitted mime={_mime(block)}]')
            continue
        if kind == 'resource':
            resource = _field(block, 'resource')
            text = _field(resource, 'text') if resource is not None else None
            if isinstance(text, str):
                parts.append(text)
            else:
                parts.append('[resource omitted]')
            continue
        parts.append('[content omitted]')
    meta: dict[str, Any] = {}
    if structured is not None:
        try:
            encoded = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        except TypeError:
            parts.append('[structuredContent omitted]')
        else:
            meta['structuredContent'] = structured
            parts.append(encoded)
    if is_error:
        meta['isError'] = True
    return ToolResult(output='\n'.join(parts), meta=meta, trust=TrustLevel.UNTRUSTED)


def _never_matches(text: str) -> bool:
    return False


# 等级按工具定，不按实参文本。matches 恒为假：恒为真会让同表里的 Echo 也命中这条规则。
_MCP_RULE = DangerRule(
    rule_id=MCP_EXTERNAL_TOOL,
    level=RiskLevel.HIGH,
    reason='外部 MCP 工具',
    matches=_never_matches,
)


class McpJudge(DangerJudge):
    """默认规则照常先匹配。都没命中、且工具声明了 MCP 风险时，结论是 `mcp.external_tool`。

    非文本的顶层实参先变成 JSON 再交给规则。数字和布尔读不出命令文本；不改写的话判定会因类型
    失败，这次调用就没有闸。改写只用于判定，不改变真正发出去的实参。
    """

    def __init__(self, inner: DangerJudge | None = None) -> None:
        rules = DangerJudge().rules if inner is None else inner.rules
        if all(rule.rule_id != MCP_EXTERNAL_TOOL for rule in rules):
            rules = (*rules, _MCP_RULE)
        super().__init__(rules)

    def judge(self, tool: Tool, args: Mapping[str, Any]) -> DangerVerdict:
        viewed = _for_rules(args) if type(tool).__dict__.get('__tool_mcp__') is True else args
        verdict = super().judge(tool, viewed)
        if verdict.rule_id != NO_RULE_MATCHED.rule_id:
            return verdict
        risk = type(tool).__dict__.get('__tool_mcp_risk__')
        if not isinstance(risk, RiskLevel):
            return verdict
        return DangerVerdict(
            rule_id=MCP_EXTERNAL_TOOL,
            level=risk,
            reason=f'外部 MCP 工具 {tool.name} 按配置与注解判为 {risk.value}',
        )


def _for_rules(args: Mapping[str, Any]) -> dict[str, Any]:
    viewed: dict[str, Any] = {}
    for key, value in args.items():
        if value is None or isinstance(value, str | bytes | bytearray | Mapping | Sequence):
            viewed[key] = value
            continue
        try:
            viewed[key] = json.dumps(value)
        except TypeError:
            viewed[key] = ''
    return viewed


def _make_tool(
    public: str,
    remote_name: str,
    description: str,
    parameters: dict[str, Any],
    risk: RiskLevel,
    invoke: Invoke,
) -> Tool:
    properties = cast(object, parameters.get('properties'))
    if isinstance(properties, dict):
        command_args = tuple(cast(Mapping[str, Any], properties))
    else:
        command_args = ()
    schema = {'name': public, 'description': description, 'parameters': parameters}

    async def execute(self: Tool, **kwargs: object) -> ToolResult:
        arguments = dict(kwargs)
        raw = cast(object, type(self).__dict__.get('__tool_schema__'))
        if isinstance(raw, dict):
            declared = cast(object, cast(Mapping[str, Any], raw).get('parameters'))
            if isinstance(declared, dict):
                rejected = _reject_args(public, cast(Mapping[str, Any], declared), arguments)
                if rejected is not None:
                    return rejected
        return await invoke(remote_name, arguments)

    cls = type(
        'Mcp_' + public.replace('-', '_'),
        (Tool,),
        {
            '__tool_name__': public,
            '__tool_schema__': schema,
            '__tool_command_args__': command_args,
            '__tool_external_input__': True,
            '__tool_mcp__': True,
            '__tool_mcp_risk__': risk,
            '__module__': __name__,
            'execute': execute,
        },
    )
    return cast(Tool, cls())


def _reject_args(
    public: str, schema: Mapping[str, Any], arguments: Mapping[str, object]
) -> ToolResult | None:
    try:
        jsonschema.validate(instance=dict(arguments), schema=dict(schema))
    except jsonschema.SchemaError as exc:
        message = exc.message
    except jsonschema.ValidationError as exc:
        message = exc.message
    else:
        return None
    return ToolResult(
        output=(
            f'期望实参符合 MCP 工具 {public!r} 的 inputSchema。'
            f'实际 {message}。'
            f'下一步：按 inputSchema 改参数后再次调用 {public!r}。'
        ),
        trust=TrustLevel.UNTRUSTED,
    )


def _inline(schema: object) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(schema, dict):
        return None, 'bad-schema'
    root = cast(dict[str, Any], schema)
    try:
        walked = _walk(root, root, 0, ())
    except _InlineFailure as exc:
        return None, exc.reason
    if not isinstance(walked, dict):
        return None, 'bad-schema'
    return cast(dict[str, Any], walked), ''


def _walk(root: Mapping[str, Any], node: object, depth: int, stack: tuple[str, ...]) -> object:
    if depth > _MAX_SCHEMA_DEPTH or len(stack) > _MAX_SCHEMA_DEPTH:
        raise _InlineFailure('too-deep')
    if isinstance(node, list):
        items = cast(list[object], node)
        return [_walk(root, item, depth + 1, stack) for item in items]
    if not isinstance(node, dict):
        return node
    mapping = cast(dict[str, Any], node)
    ref = cast(object, mapping.get('$ref'))
    if isinstance(ref, str):
        if ref in stack:
            raise _InlineFailure('cyclic-$ref')
        target = _resolve(root, ref)
        if target is _REJECT:
            raise _InlineFailure('remote-$ref')
        if target is None:
            raise _InlineFailure('missing-$ref')
        resolved = _walk(root, target, depth + 1, stack + (ref,))
        extra = {key: value for key, value in mapping.items() if key != '$ref'}
        if not extra:
            return resolved
        if not isinstance(resolved, dict):
            raise _InlineFailure('bad-schema')
        merged = dict(cast(dict[str, Any], resolved))
        for key, value in extra.items():
            merged[key] = _walk(root, value, depth + 1, stack)
        return merged
    if ref is not None:
        raise _InlineFailure('bad-schema')
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if depth == 0 and key in ('$defs', 'definitions'):
            continue
        out[key] = _walk(root, value, depth + 1, stack)
    return out


def _resolve(root: Mapping[str, Any], ref: str) -> object:
    if not ref.startswith('#') or (ref != '#' and not ref.startswith('#/')):
        return _REJECT
    current: Any = root
    if ref == '#':
        return current
    for part in ref[2:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        if not isinstance(current, Mapping):
            return None
        mapping = cast(Mapping[str, Any], current)
        if part not in mapping:
            return None
        current = mapping[part]
    return current


def _field(block: object, name: str) -> object:
    if isinstance(block, Mapping):
        return cast(Mapping[object, object], block).get(name)
    return getattr(block, name, None)


def _mime(block: object) -> object:
    mime = _field(block, 'mime_type')
    if mime is None:
        mime = _field(block, 'mimeType')
    return mime


def _log_excluded(item: ExcludedTool) -> None:
    logger.error('excluded MCP tool %s %s %s', item.server, item.name, item.reason)
