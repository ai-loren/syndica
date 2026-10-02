"""OTLP 导出的端点、头与资源属性从哪来（M3 D14）。导出器本身在 backend 里，由组合根接给宿主。

端点只从用户自己的环境或命令行来，项目配置不能设：仓库可能来自不受信任的人，它选的端点会收到 trace
的结构（命令、路径、工具名）与用户的鉴权头。`OTEL_EXPORTER_OTLP_HEADERS` 与
`OTEL_EXPORTER_OTLP_TRACES_HEADERS` 只随环境里的端点发出，`trace export --otlp` 给的端点不带它们；
明文 HTTP 的非回环地址不带头，也不许在地址里带用户名与密码。OpenTelemetry 的开关照它的约定认：
`OTEL_SDK_DISABLED=true` 与 `OTEL_TRACES_EXPORTER=none` 关掉导出，协议是 grpc 时不导出（这里只会
OTLP/HTTP）。
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import SplitResult, unquote, urlsplit

from syndica.kernel import ConfigError, TraceStore
from syndica.kernel.redaction import Redactor

__all__ = [
    'HEADER_VARIABLES',
    'ExportTarget',
    'TraceExporter',
    'command_line_target',
    'environment_resource',
    'environment_target',
    'header_values',
]

_TRACES_ENDPOINT = 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'
_ENDPOINT = 'OTEL_EXPORTER_OTLP_ENDPOINT'
_HEADERS = 'OTEL_EXPORTER_OTLP_HEADERS'
_TRACES_HEADERS = 'OTEL_EXPORTER_OTLP_TRACES_HEADERS'
_PROTOCOL = 'OTEL_EXPORTER_OTLP_PROTOCOL'
_TRACES_PROTOCOL = 'OTEL_EXPORTER_OTLP_TRACES_PROTOCOL'
# 通用的在前、按信号的在后：同名的头按信号的那份算。
HEADER_VARIABLES = (_HEADERS, _TRACES_HEADERS)
_TRACES_PATH = '/v1/traces'
# RFC 9110 的 token：头名只许这些字符；头的值只许可见的 ASCII、空格与制表符。
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE = re.compile(r'[\t\x20-\x7e]*')
# 鉴权头的值常是「方案 凭证」（`Bearer xxx`）：凭证单独也交给脱敏器，它单独出现时照样换掉。
_AUTH_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9!#$%&\'*+.^_`|~-]*\s+(\S+)')


class TraceExporter(Protocol):
    def __call__(
        self,
        store: TraceStore,
        trace_id: str,
        *,
        url: str,
        headers: Mapping[str, str],
        resource: Mapping[str, str],
        redactor: Redactor,
    ) -> Awaitable[int]: ...


@dataclass(frozen=True)
class ExportTarget:
    """发往哪里（完整的 traces 端点）、带哪些头；`note` 是要告诉用户的一句（头没有发出去的
    原因）。"""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    note: str | None = None


def environment_target(environ: Mapping[str, str]) -> ExportTarget | None:
    """用户环境里的端点：`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 原样用，
    `OTEL_EXPORTER_OTLP_ENDPOINT` 补上 `/v1/traces`（OpenTelemetry 的约定）；没设、或被
    `OTEL_SDK_DISABLED` / `OTEL_TRACES_EXPORTER` 关掉时 None。端点、头或协议用不了时报
    `ConfigError`。"""
    if _disabled(environ):
        return None
    specific = environ.get(_TRACES_ENDPOINT, '').strip()
    generic = environ.get(_ENDPOINT, '').strip()
    if specific:
        parts = _checked(specific, _TRACES_ENDPOINT)
        url = parts.geturl()
    elif generic:
        parts = _checked(generic, _ENDPOINT)
        url = _traces_url(parts)
    else:
        return None
    _checked_protocol(environ)
    _refuse_plain_credentials(parts, _TRACES_ENDPOINT if specific else _ENDPOINT)
    headers: dict[str, str] = {}
    for variable in HEADER_VARIABLES:
        headers.update(_parsed_headers(environ.get(variable, ''), variable))
    if headers and not _protected(parts):
        return ExportTarget(
            url,
            note=(
                f'{" 与 ".join(HEADER_VARIABLES)} 里的头没有随导出发出：'
                '端点是明文 HTTP 且不在本机，头里的凭证会在网络上被看到。要带头就把端点换成 https'
            ),
        )
    return ExportTarget(url, headers)


def command_line_target(url: str) -> ExportTarget:
    """`trace export --otlp <url>`：缺 `/v1/traces` 时补上；不带环境里的头（那是给环境里的端点的）。
    明文 HTTP 的非回环地址里带着用户名或密码就拒：httpx 会把它们当鉴权头明文发出。"""
    parts = _checked(url, '--otlp')
    _refuse_plain_credentials(parts, '--otlp')
    return ExportTarget(_traces_url(parts))


def environment_resource(environ: Mapping[str, str]) -> dict[str, str]:
    """`OTEL_RESOURCE_ATTRIBUTES`（逗号分隔的 key=value，值按 URL 编码）与 `OTEL_SERVICE_NAME`
    （同名时盖过前者）。写错时报 `ConfigError`，不回显值。"""
    resource: dict[str, str] = {}
    raw = environ.get('OTEL_RESOURCE_ATTRIBUTES', '')
    for number, item in enumerate(raw.split(','), 1):
        if not item.strip():
            continue
        key, separator, value = item.partition('=')
        if not separator or not key.strip():
            raise ConfigError(
                expected='OTEL_RESOURCE_ATTRIBUTES 是逗号分隔的 key=value（值按 URL 编码）',
                received=f'第 {number} 项没有「key=」（值不在这里回显）',
                next_action=f'改正 OTEL_RESOURCE_ATTRIBUTES 的第 {number} 项',
            )
        resource[key.strip()] = unquote(value.strip())
    service = environ.get('OTEL_SERVICE_NAME', '').strip()
    if service:
        resource['service.name'] = service
    return resource


def header_values(raw: str) -> list[str]:
    """`OTEL_EXPORTER_OTLP_*HEADERS` 里的每个值（解码前后两份，带鉴权方案的另交方案之后的凭证），
    交给脱敏器逐字换掉；写错的项也尽量取出来，这里不报错。"""
    values: list[str] = []
    for item in raw.split(','):
        _, separator, value = item.partition('=')
        if not separator:
            continue
        for form in {value.strip(), unquote(value.strip())}:
            values.append(form)
            scheme = _AUTH_SCHEME.fullmatch(form)
            if scheme is not None:
                values.append(scheme.group(1))
    return values


def _disabled(environ: Mapping[str, str]) -> bool:
    if environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
        return True
    exporters = environ.get('OTEL_TRACES_EXPORTER', '').strip().lower()
    if not exporters:
        return False
    return 'otlp' not in {name.strip() for name in exporters.split(',')}


def _checked_protocol(environ: Mapping[str, str]) -> None:
    variable = _TRACES_PROTOCOL if environ.get(_TRACES_PROTOCOL, '').strip() else _PROTOCOL
    protocol = environ.get(variable, '').strip().lower()
    if protocol == 'grpc':
        raise ConfigError(
            expected=f'{variable} 是 http/json 或 http/protobuf（syndica 只会 OTLP/HTTP）',
            received='它是 grpc',
            next_action=(
                f'把 {variable} 设成 http/json、端点指向 collector 的 HTTP 端口（默认 4318）；'
                '不要 syndica 导出就设 OTEL_TRACES_EXPORTER=none'
            ),
        )


def _refuse_plain_credentials(parts: SplitResult, source: str) -> None:
    if (parts.username or parts.password) and not _protected(parts):
        raise ConfigError(
            expected=f'{source} 的端点里带凭证时是 https 或本机地址',
            received='端点是明文 HTTP 的非本机地址，还带着用户名或密码',
            next_action='换成 https，或者把凭证改放进 OTEL_EXPORTER_OTLP_HEADERS',
        )


def _parsed_headers(raw: str, variable: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for number, item in enumerate(raw.split(','), 1):
        if not item.strip():
            continue
        name, separator, value = item.partition('=')
        name, value = name.strip(), unquote(value.strip())
        problem = None
        if not separator:
            problem = '没有「=」'
        elif not _HEADER_NAME.fullmatch(name):
            problem = '头名不是合法的 HTTP 头名'
        elif not _HEADER_VALUE.fullmatch(value):
            problem = '值里有换行、控制字符或 ASCII 之外的字符'
        if problem is not None:
            raise ConfigError(
                expected=f'{variable} 是逗号分隔的 name=value（值按 URL 编码）',
                received=f'第 {number} 项{problem}（值不在这里回显）',
                next_action=f'改正 {variable} 的第 {number} 项',
            )
        headers[name] = value
    return headers


def _checked(url: str, source: str) -> SplitResult:
    parts = _split(url)
    if parts is None or parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ConfigError(
            expected=f'{source} 是 http:// 或 https:// 开头、带主机名的地址',
            received='它不是（值不在这里回显：它可能带着凭证）',
            next_action='改成形如 http://localhost:4318 的地址再试',
        )
    return parts


def _split(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
        _ = parts.port  # 端口不是数字或越界时这里才抛
        if parts.hostname:
            parts.hostname.encode('idna')
    except (ValueError, UnicodeError):
        return None
    return parts


def _traces_url(parts: SplitResult) -> str:
    path = parts.path.rstrip('/')
    if not path.endswith(_TRACES_PATH):
        path = f'{path}{_TRACES_PATH}'
    return parts._replace(path=path).geturl()


def _protected(parts: SplitResult) -> bool:
    """https，或本机地址（localhost、回环地址）：头在这两种情况下不会以明文出现在网络上。"""
    if parts.scheme == 'https':
        return True
    host = parts.hostname or ''
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
