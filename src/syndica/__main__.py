"""`python -m syndica` entry point."""

from __future__ import annotations

import os
import re
import signal
import sys
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from syndica.cli.main import (
    EXIT_INTERRUPTED,
    CliIO,
    CliOutcome,
    InspectRequest,
    MemoryRequest,
    RunRequest,
    SessionsRequest,
    TraceRequest,
    main,
)

if TYPE_CHECKING:
    from syndica.cli.config import CliConfig
    from syndica.cli.host import RuntimeCliHost
    from syndica.cli.mcp_session import OpenedMcp
    from syndica.cli.trace_commands import Replayed
    from syndica.kernel import LLM, DangerJudge, ScenarioDocument, TraceRecorder, TraceStore
    from syndica.kernel.budget import PriceTable
    from syndica.kernel.redaction import Redactor

_OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
_OPENROUTER_DEFAULT_MODEL = 'openrouter/free'
_GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai'
_GEMINI_DEFAULT_MODEL = 'gemini-3.5-flash-lite'
# Gemini 3.x 拒收没有 thought_signature 的函数调用（HTTP 400）。轮换时历史里的调用可能由另一家
# 平台答出，挂上 Google 约定的这个占位值，Gemini 才接得住那段历史。
_GEMINI_FOREIGN_TOOL_CALL_EXTRA = {
    'google': {'thought_signature': 'skip_thought_signature_validator'}
}
_ORDER_ENV = 'SYNDICA_LLM_ORDER'
# 清单 model 的 `<平台>:<型号>`：第一个 `:` 之前形如一个名字（没有 `/` 与 `.`）时它就是平台
# 前缀，且只能是这三个之一。命名空间是封闭的：未知前缀此刻就报错，以后加平台不会改变任何已有字符串
# 的含义。只收 ASCII：全角冒号、看起来像拉丁字母的西里尔字母会让前缀检查认不出平台名。
# `<平台>:default` 是那一家在环境里配置的缺省型号。
_PLATFORMS = ('gemini', 'openrouter', 'openai')
_PLATFORM_PREFIX = re.compile(r'\s*[A-Za-z][A-Za-z0-9_-]*\s*:')
# 不设 SYNDICA_LLM_ORDER 时进轮换的平台及顺序。通用的 OPENAI_* 端点不在其中：它可能是付费端点，
# 只在这两家都没配置时单独使用，或在 SYNDICA_LLM_ORDER 里被显式点名。清单（项目配置，不可信）
# 点名它同样要满足这一条，见 `_require_generic_endpoint`。
_DEFAULT_RING = ('gemini', 'openrouter')


def _free_openrouter_model(value: str | None, *, source: str) -> str:
    from syndica.kernel import ConfigError

    if not value or not value.strip():
        raise ConfigError(
            expected=f'{source} 是 openrouter/free 或以 :free 结尾的模型 ID',
            received=f'收到 {source}={value!r}',
            next_action=f'删除 {source} 使用 openrouter/free，或设置一个明确的 :free 模型',
        )
    if value != _OPENROUTER_DEFAULT_MODEL and not value.endswith(':free'):
        raise ConfigError(
            expected='OpenRouter 只使用 openrouter/free 或以 :free 结尾的免费模型',
            received=f'{source} 是 {value!r}',
            next_action=(
                f'把 {source} 改成 openrouter/free，或从 OpenRouter 免费模型列表选择一个 :free 模型'
            ),
        )
    return value


def _is_openrouter_url(value: str) -> bool:
    try:
        return urlsplit(value).hostname == 'openrouter.ai'
    except ValueError:
        return False


def _openrouter_headers() -> dict[str, str]:
    return {
        'HTTP-Referer': 'https://github.com/ai-loren/syndica',
        'X-Title': 'Syndica',
    }


def _gemini_configured() -> bool:
    return 'GEMINI_API_KEY' in os.environ or 'GEMINI_MODEL' in os.environ


def _openrouter_configured() -> bool:
    return 'OPENROUTER_API_KEY' in os.environ or 'OPENROUTER_MODEL' in os.environ


def _openai_configured() -> bool:
    return 'OPENAI_BASE_URL' in os.environ


_CONFIGURED = {
    'gemini': _gemini_configured,
    'openrouter': _openrouter_configured,
    'openai': _openai_configured,
}
_HOW_TO_CONFIGURE = {
    'gemini': '设置 GEMINI_API_KEY',
    'openrouter': '设置 OPENROUTER_API_KEY',
    'openai': '设置 OPENAI_BASE_URL 与 OPENAI_MODEL',
}


def _ring_order() -> tuple[str, ...]:
    from syndica.kernel import ConfigError

    raw = os.environ.get(_ORDER_ENV)
    if raw is None:
        return tuple(name for name in _DEFAULT_RING if _CONFIGURED[name]()) or ('openai',)
    names = tuple(part.strip() for part in raw.split(','))
    if not all(names):
        raise ConfigError(
            expected=f'{_ORDER_ENV} 是逗号分隔的平台名，不含空项',
            received=f'收到 {_ORDER_ENV}={raw!r}',
            next_action=f'写成 gemini,openrouter 这样的列表；可用的平台名是 {list(_CONFIGURED)}',
        )
    unknown = [name for name in names if name not in _CONFIGURED]
    if unknown:
        raise ConfigError(
            expected=f'{_ORDER_ENV} 里的平台名是 {list(_CONFIGURED)} 之一',
            received=f'收到未知平台 {unknown!r}',
            next_action='改成上面列出的平台名，或把它删掉',
        )
    duplicates = sorted({name for index, name in enumerate(names) if name in names[:index]})
    if duplicates:
        raise ConfigError(
            expected=f'{_ORDER_ENV} 里每个平台只出现一次',
            received=f'平台 {duplicates!r} 重复',
            next_action='删掉重复项；每个平台在轮换里只占一个位置',
        )
    missing = [name for name in names if not _CONFIGURED[name]()]
    if missing:
        fixes = '；'.join(f'{name}：{_HOW_TO_CONFIGURE[name]}' for name in missing)
        raise ConfigError(
            expected=f'{_ORDER_ENV} 点名的平台都已配置',
            received=f'平台 {missing!r} 没有配置',
            next_action=f'{fixes}；或者把它从 {_ORDER_ENV} 里删掉',
        )
    return names


def _gemini_model(name: str) -> LLM:
    from syndica.backends.llm_openai import OpenAICompatible
    from syndica.kernel import ConfigError, LLMConfig

    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key or not api_key.strip():
        raise ConfigError(
            expected='GEMINI_API_KEY 是非空的 Gemini API key',
            received='GEMINI_API_KEY 未设置或为空',
            next_action='设置 GEMINI_API_KEY 后重试；不要把 key 写进配置文件或仓库',
        )
    return OpenAICompatible(
        LLMConfig(
            provider='openai-compatible',
            model=(
                os.environ.get('GEMINI_MODEL', '').strip() or _GEMINI_DEFAULT_MODEL
                if name == 'default'
                else name
            ),
            client_args={
                'base_url': _GEMINI_BASE_URL,
                'api_key': api_key,
                'foreign_tool_call_extra': _GEMINI_FOREIGN_TOOL_CALL_EXTRA,
            },
        )
    )


def _platform_and_model(name: str) -> tuple[str | None, str]:
    """清单的 model → (平台, 型号)；没有平台前缀时平台是 None。按**第一个** `:` 切。"""
    from syndica.kernel import ConfigError

    if not name.isascii():
        odd = ', '.join(f'U+{ord(char):04X}' for char in sorted(set(name)) if not char.isascii())
        raise ConfigError(
            expected='清单 model 只含 ASCII 字符（平台前缀与模型 ID 都是 ASCII）',
            received=f'收到 model={name!r}，其中 {odd} 不是 ASCII',
            next_action=(
                '把这些字符换成 ASCII：全角冒号写成 :，看起来像拉丁字母的别的字母换成拉丁字母'
            ),
        )
    if _PLATFORM_PREFIX.match(name) is None:
        return None, name
    prefix, _, model = name.partition(':')
    if prefix not in _PLATFORMS:
        canonical = prefix.strip().lower()
        fix = (
            f'改成 {canonical}:{model}'
            if canonical in _PLATFORMS
            else (
                f'它若是带标签的模型名（如 Ollama 的 qwen3:8b），写成 openai:{name}，'
                f'经 OPENAI_BASE_URL 调用；平台名写错了就改成 {list(_PLATFORMS)} 之一'
            )
        )
        raise ConfigError(
            expected=f'清单 model 的平台前缀是 {list(_PLATFORMS)} 之一，小写、不带空白',
            received=f'收到 model={name!r}，前缀是 {prefix!r}',
            next_action=fix,
        )
    if not model.strip():
        raise ConfigError(
            expected=f'清单 model 写成 {prefix}:<型号>，型号非空',
            received=f'收到 model={name!r}',
            next_action=(
                f'在冒号后面写上 {prefix} 平台上的型号；用平台的缺省型号就把 model 写成 default'
            ),
        )
    if model != model.strip():
        raise ConfigError(
            expected=f'清单 model 写成 {prefix}:<型号>，冒号后面紧跟型号',
            received=f'收到 model={name!r}，型号前后带空白',
            next_action=f'写成 {prefix}:{model.strip()}',
        )
    return prefix, model


def _require_generic_endpoint(name: str, model: str) -> None:
    """清单来自项目配置，而通用的 OPENAI_* 端点可能付费：要用户自己的环境选了它——`SYNDICA_LLM_ORDER`
    里列了 openai，或两家免费平台都没配置、它本来就是唯一的平台。"""
    from syndica.kernel import ConfigError

    if 'openai' in _ring_order():
        return
    openrouter = (
        ''
        if _openrouter_configured()
        else '设置 OPENROUTER_API_KEY 让不带前缀的名字走 OpenRouter（只收 :free 模型）；'
    )
    raise ConfigError(
        expected='清单的 model 走通用的 OPENAI_* 端点之前，你自己的环境选了它（它可能是付费端点）',
        received=(
            f'model={name!r} 要走 OPENAI_*，而已配置了 Gemini 或 OpenRouter，'
            f'{_ORDER_ENV} 里也没有 openai'
        ),
        next_action=(
            f'要用 Gemini 就写成 gemini:{model}；{openrouter}'
            f'确实要走 OPENAI_* 就在 {_ORDER_ENV} 里列上 openai（如 gemini,openrouter,openai）'
        ),
    )


def _default_prices() -> PriceTable:
    """backend 认得的默认价格（只有免费模型）；`.syndica.yaml` 的 prices: 由宿主排在它前面。"""
    from syndica.backends.llm_openai import FreeTierPrices

    return FreeTierPrices()


def _configured_model(name: str) -> LLM:
    """角色清单里的模型名 → LLM。

    交出去的总是 `ModelRouter`：失败的平台按退避重试，配了多个平台时还会换平台。`default` 由已
    配置的平台按顺序组成轮换；`<平台>:<型号>` 只走那一家；没有平台前缀的模型名仍按原规则走
    OpenRouter（配置了的话）或 `OPENAI_*`。后两种都不进轮换。
    """
    from syndica.backends.llm_openai import recoverable, retry_after
    from syndica.backends.llm_router import ModelRouter

    platform, model = _platform_and_model(name)
    if platform == 'gemini':
        members = [('gemini', _gemini_model(model))]
    elif platform == 'openrouter':
        members = [('openrouter', _openrouter_model(model))]
    elif platform == 'openai':
        _require_generic_endpoint(name, model)
        members = [('openai', _openai_model(model))]
    elif name != 'default':
        if _openrouter_configured():
            members = [('openrouter', _openrouter_model(name))]
        else:
            _require_generic_endpoint(name, name)
            members = [('openai', _openai_model(name))]
    else:
        builders = {
            'gemini': lambda: _gemini_model('default'),
            'openrouter': lambda: _openrouter_model('default'),
            'openai': lambda: _openai_model('default'),
        }
        members = [(platform, builders[platform]()) for platform in _ring_order()]
    return ModelRouter(members, retry_after=retry_after, recoverable=recoverable)


def _openrouter_model(name: str) -> LLM:
    from syndica.backends.llm_openai import OpenAICompatible
    from syndica.kernel import ConfigError, LLMConfig

    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key or not api_key.strip():
        raise ConfigError(
            expected='OPENROUTER_API_KEY 是非空的 OpenRouter API key',
            received='OPENROUTER_API_KEY 未设置或为空',
            next_action='设置 OPENROUTER_API_KEY 后重试；不要把 key 写进配置文件或仓库',
        )
    model = _free_openrouter_model(
        (
            os.environ.get('OPENROUTER_MODEL', _OPENROUTER_DEFAULT_MODEL)
            if name == 'default'
            else name
        ),
        source='OPENROUTER_MODEL' if name == 'default' else '清单的 model',
    )
    return OpenAICompatible(
        LLMConfig(
            provider='openai-compatible',
            model=model,
            client_args={
                'base_url': _OPENROUTER_BASE_URL,
                'api_key': api_key,
                'headers': _openrouter_headers(),
            },
        )
    )


def _openai_model(name: str) -> LLM:
    from syndica.backends.llm_openai import OpenAICompatible
    from syndica.kernel import ConfigError, LLMConfig

    base_url = os.environ.get('OPENAI_BASE_URL')
    model = os.environ.get('OPENAI_MODEL') if name == 'default' else name
    if not base_url or not base_url.strip():
        alternative = (
            '用 Gemini 或 OpenRouter 就改设 GEMINI_API_KEY 或 OPENROUTER_API_KEY'
            if name == 'default'
            else (
                f'或设置 OPENROUTER_API_KEY 让 {name!r} 走 OpenRouter；'
                f'要用 Gemini 就把清单的 model 写成 gemini:{name}'
            )
        )
        raise ConfigError(
            expected='OPENAI_BASE_URL 是非空的 OpenAI-compatible API 根 URL',
            received=f'收到 OPENAI_BASE_URL={base_url!r}',
            next_action=(
                f'设置 OPENAI_BASE_URL，例如 https://host/v1，再重试 run/chat/doctor；{alternative}'
            ),
        )
    if not model or not model.strip():
        raise ConfigError(
            expected='默认模型由非空 OPENAI_MODEL 指定',
            received=f'收到 OPENAI_MODEL={model!r}',
            next_action='设置 OPENAI_MODEL；清单显式 model 字段不使用这个默认值',
        )
    if _is_openrouter_url(base_url):
        model = _free_openrouter_model(
            model, source='OPENAI_MODEL' if name == 'default' else '清单的 model'
        )
    client_args: dict[str, object] = {'base_url': base_url}
    api_key = os.environ.get('OPENAI_API_KEY')
    if api_key:
        client_args['api_key'] = api_key
    if _is_openrouter_url(base_url):
        client_args['headers'] = _openrouter_headers()
    return OpenAICompatible(
        LLMConfig(
            provider='openai-compatible',
            model=model,
            client_args=client_args,
        )
    )


def _replay_document(
    document: ScenarioDocument,
    *,
    redactor: Redactor,
    allow_divergence: bool,
    recorder: TraceRecorder | None,
    config_file: Path | None,
) -> Replayed:
    """`trace replay` 的回放：CLI 与 runner 互不依赖，由组合根把两边接上。第三方 Module 按
    当前工作区与 `--config` 给的那份配置（受信任的配置、装着的发行版）判能不能重建（D13）。"""
    from syndica.cli.modules import installed_entry_points
    from syndica.cli.replay_modules import module_rebuilder
    from syndica.runner.replay import ReplayRunner

    rebuilder = module_rebuilder(
        workspace_root=Path.cwd(),
        environ=os.environ,
        entry_points=installed_entry_points,
        config_file=config_file,
    )
    return ReplayRunner(
        redactor=redactor, allow_divergence=allow_divergence, modules=rebuilder
    ).run(document, recorder=recorder)


async def _export_trace(
    store: TraceStore,
    trace_id: str,
    *,
    url: str,
    headers: Mapping[str, str],
    resource: Mapping[str, str],
    redactor: Redactor,
) -> int:
    """OTLP 导出：CLI 不 import backend，由组合根接上；httpx 是 extra `otel`。"""
    try:
        from syndica.backends.otel_otlp import OtlpHttpExporter
    except ImportError as error:
        from syndica.kernel import ConfigError

        raise ConfigError(
            expected='OTLP 导出要的 httpx 已经装好',
            received=f'导入失败：{error}',
            next_action="pip install 'syndica[otel]' 之后再试",
        ) from error
    exporter = OtlpHttpExporter(url, redactor=redactor, headers=headers, resource=resource)
    return await exporter.export(store, trace_id)


async def _open_mcp(
    config: CliConfig,
    agent_env: Mapping[str, str],
    *,
    cwd: str,
    redactor: Redactor,
    environ: Mapping[str, str],
    judge: DangerJudge,
    workspace: str,
) -> OpenedMcp | None:
    """组合根把 MCP backend 接上。没有声明的服务时不导入额外依赖。"""
    if not config.interop.mcp_servers:
        return None
    try:
        from syndica.backends.mcp.client import connect_http, connect_stdio
        from syndica.backends.mcp.module import assign_roles, mcp_module
        from syndica.backends.mcp.tools import McpJudge
    except ImportError as exc:
        from syndica.kernel import ConfigError

        raise ConfigError(
            expected="MCP 客户端已安装（pip install 'syndica[mcp]'）",
            received=f'导入失败：{exc}',
            next_action="运行 pip install 'syndica[mcp]' 后重试",
        ) from exc
    from syndica.cli.mcp_session import McpPorts, open_mcp_clients

    return await open_mcp_clients(
        config,
        agent_env,
        cwd=cwd,
        redactor=redactor,
        environ=environ,
        judge=judge,
        workspace=workspace,
        ports=McpPorts(
            connect_stdio=connect_stdio,
            connect_http=connect_http,
            assign_roles=assign_roles,
            make_module=mcp_module,
            wrap_judge=McpJudge,
        ),
    )


# 组合根在解析模型名之前把这份配置里的 ACP Agent 放进来。工厂只拿到名字，命令在这里。
_ACP_SPECS: ContextVar[Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] | None] = ContextVar(
    'syndica_acp_specs', default=None
)


def _remember_acp(config: CliConfig, workspace: Path, environ: Mapping[str, str]) -> None:
    from syndica.cli.interop_config import (
        InteropCredential,
        credential_for,
        interop_credentials_path,
        load_interop_credentials,
    )

    credentials: Mapping[str, InteropCredential] = {}
    path = interop_credentials_path(environ)
    if path is not None:
        credentials = load_interop_credentials(path, workspace=workspace)
    specs: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for agent in config.interop.acp_agents:
        extra = agent.pass_env
        cred = credential_for(credentials, agent)
        if cred is not None:
            extra = (*extra, *cred.env)
        specs[agent.name] = (agent.command, extra)
    _ACP_SPECS.set(specs)


def _acp_model(name: str) -> LLM:
    from syndica.kernel import ConfigError

    key = name.split(':', 1)[1]
    specs = _ACP_SPECS.get()
    if specs is None or key not in specs:
        declared = () if specs is None else tuple(sorted(specs))
        raise ConfigError(
            expected=f'{name} 指向这份配置的 interop.acp.agents 里已声明的名字',
            received=f'没有 {key!r}；已声明 {declared!r}',
            next_action=(
                '在 interop.acp.agents 里加上这个 name，或把角色的 model 改成已经声明的 acp:<名字>'
            ),
        )
    command, extra = specs[key]
    try:
        from syndica.backends.acp import AcpAgentModel
    except ImportError as exc:
        raise ConfigError(
            expected="ACP 客户端已安装（pip install 'syndica[acp]'）",
            received=f'导入失败：{exc}',
            next_action="运行 pip install 'syndica[acp]' 后重试",
        ) from exc
    return AcpAgentModel(key, command=command, pass_env=extra)


_A2A_SPECS: ContextVar[Mapping[str, tuple[str, str | None]] | None] = ContextVar(
    'syndica_a2a_specs', default=None
)


def _remember_a2a(config: CliConfig, workspace: Path, environ: Mapping[str, str]) -> None:
    from syndica.cli.interop_config import (
        InteropCredential,
        credential_for,
        interop_credentials_path,
        load_interop_credentials,
    )
    from syndica.kernel import ConfigError

    credentials: Mapping[str, InteropCredential] = {}
    path = interop_credentials_path(environ)
    if path is not None:
        credentials = load_interop_credentials(path, workspace=workspace)
    specs: dict[str, tuple[str, str | None]] = {}
    for agent in config.interop.a2a_agents:
        cred = credential_for(credentials, agent)
        bearer: str | None = None
        if cred is not None and cred.bearer_env:
            variable = cred.bearer_env
            value = environ.get(variable)
            if value is None or not value:
                raise ConfigError(
                    expected=f'环境变量 {variable} 有值，用作 A2A Agent {agent.name!r} 的 bearer',
                    received=f'{variable} 没有设置',
                    next_action=(
                        f'在启动 Syndica 的环境里设置 {variable}，或从用户级 interop.yaml 删掉 '
                        f'a2a:{agent.name} 这一项（它的 bearer_env 点名了 {variable}）'
                    ),
                )
            bearer = value
        specs[agent.name] = (agent.url, bearer)
    _A2A_SPECS.set(specs)


def _a2a_model(name: str) -> LLM:
    from syndica.kernel import ConfigError

    key = name.split(':', 1)[1]
    specs = _A2A_SPECS.get()
    if specs is None or key not in specs:
        declared = () if specs is None else tuple(sorted(specs))
        raise ConfigError(
            expected=f'{name} 指向这份配置的 interop.a2a.agents 里已声明的名字',
            received=f'没有 {key!r}；已声明 {declared!r}',
            next_action=(
                '在 interop.a2a.agents 里加上这个 name，或把角色的 model 改成已经声明的 a2a:<名字>'
            ),
        )
    url, bearer = specs[key]
    try:
        from syndica.backends.a2a import A2AAgentModel
    except ImportError as exc:
        raise ConfigError(
            expected="A2A 客户端已安装（pip install 'syndica[a2a]'）",
            received=f'导入失败：{exc}',
            next_action="运行 pip install 'syndica[a2a]' 后重试",
        ) from exc
    return A2AAgentModel(key, url, bearer=bearer)


def build_host(io: CliIO, workspace_root: Path) -> RuntimeCliHost:
    """CLI 与 server 用同一个宿主：模型、价格、回放、导出、信任都在这里接一次。"""
    from syndica.cli.host import RuntimeCliHost
    from syndica.cli.modules import installed_entry_points
    from syndica.cli.trust import real_trust_gate

    def prepare(config: CliConfig) -> None:
        _remember_acp(config, workspace_root, os.environ)
        _remember_a2a(config, workspace_root, os.environ)

    def model_factory(name: str) -> LLM:
        if name.startswith('acp:'):
            return _acp_model(name)
        if name.startswith('a2a:'):
            return _a2a_model(name)
        return _configured_model(name)

    return RuntimeCliHost(
        io=io,
        model_factory=model_factory,
        workspace_root=workspace_root,
        prepare_models=prepare,
        replayer=_replay_document,
        exporter=_export_trace,
        entry_points=installed_entry_points,
        trust=real_trust_gate(),
        prices=_default_prices(),
        mcp_opener=_open_mcp,
    )


class _ServerControl:
    """`up` / `down` / `status`：server 依赖（starlette、uvicorn）只在用到时才导入。"""

    def __init__(self, io: CliIO) -> None:
        self._io = io

    def up(
        self,
        config: str,
        *,
        port: int,
        foreground: bool,
        open_browser: bool,
        api_token: bool = False,
    ) -> str | CliOutcome:
        from syndica.serve import up

        return up(
            self._io,
            Path.cwd(),
            os.environ,
            config=config,
            port=port,
            foreground=foreground,
            open_browser=open_browser,
            api_token=api_token,
        )

    def down(self) -> str | CliOutcome:
        from syndica.serve import down

        return down(Path.cwd(), os.environ)

    def status(self) -> str | CliOutcome:
        from syndica.serve import status

        return status(Path.cwd(), os.environ)

    def acp(self, config: str) -> str:
        """stdio ACP agent。Agent 的命令环境里拒绝：stdout 只放协议。"""
        from syndica.cli.environment import AGENT_MARKER
        from syndica.kernel import ConfigError
        from syndica.serve import acp_stdio

        if os.environ.get(AGENT_MARKER) == '1':
            raise ConfigError(
                expected='syndica acp 在人的环境里跑，stdout 只放协议',
                received=f'环境里 {AGENT_MARKER}=1，这是 Agent 的命令环境',
                next_action='不要在 Agent 的命令里启动 syndica acp；在人的终端里跑',
            )
        host = build_host(self._io, Path.cwd())
        try:
            return acp_stdio(host, config)
        finally:
            host.close()

    def mcp(self, config: str) -> str:
        """stdio MCP server。Agent 的命令环境里拒绝：stdout 只放协议，不能再套一层 Agent。"""
        from syndica.cli.environment import AGENT_MARKER
        from syndica.kernel import ConfigError
        from syndica.serve import mcp_stdio

        if os.environ.get(AGENT_MARKER) == '1':
            raise ConfigError(
                expected='syndica mcp 在人的环境里跑，stdout 只放协议',
                received=f'环境里 {AGENT_MARKER}=1，这是 Agent 的命令环境',
                next_action='不要在 Agent 的命令里启动 syndica mcp；在人的终端里跑',
            )
        host = build_host(self._io, Path.cwd())
        try:
            # 空串：stdio 的 stdout 只放协议，进程退出时不再补一行说明。
            return mcp_stdio(host, config)
        finally:
            host.close()


class _LazyHost:
    def __init__(self, io: CliIO) -> None:
        self._io = io
        self._host: RuntimeCliHost | None = None

    def _get(self) -> RuntimeCliHost:
        if self._host is None:
            self._host = build_host(self._io, Path.cwd())
        return self._host

    def runtime_host(self) -> RuntimeCliHost:
        return self._get()

    def run_request(self, request: RunRequest) -> str | CliOutcome:
        return self._get().run_request(request)

    def inspect(self, request: InspectRequest) -> str:
        return self._get().inspect(request)

    def trust_config(self, config: str, sha256: str | None) -> str:
        return self._get().trust_config(config, sha256)

    def doctor(self, config: str, *, verify: bool = False) -> str | CliOutcome:
        return self._get().doctor(config, verify=verify)

    def open_chat(
        self,
        config: str,
        max_turns: int | None = None,
        *,
        trace: str | None = None,
        argv: Sequence[str] = (),
        trusted_output: Sequence[str] = (),
        resume: str | None = None,
        goal: str | None = None,
        on_exhausted: str | None = None,
    ) -> str:
        # 只在给了 --trust-module-output / --goal / --on-exhausted 时才传：没有这几个参数的宿主实现
        # 照样能开 chat。
        extra: dict[str, Any] = {'trusted_output': tuple(trusted_output)} if trusted_output else {}
        if goal is not None:
            extra['goal'] = goal
        if on_exhausted is not None:
            extra['on_exhausted'] = on_exhausted
        return self._get().open_chat(
            config, max_turns, trace=trace, argv=argv, resume=resume, **extra
        )

    def active_agents(self) -> str:
        return self._get().active_agents()

    def trace(self, request: TraceRequest) -> str | CliOutcome:
        return self._get().trace(request)

    def sessions(self, request: SessionsRequest) -> str | CliOutcome:
        return self._get().sessions(request)

    def memory(self, request: MemoryRequest) -> str | CliOutcome:
        return self._get().memory(request)

    def close(self, *, interrupted: bool = False) -> None:
        if self._host is not None:
            self._host.close(interrupted=interrupted)


class _Training:
    """`syndica batch` / `syndica export`。runner 只在这里 import。"""

    def __init__(self, host: _LazyHost) -> None:
        self._host = host

    def batch(self, dataset: str, config: str) -> CliOutcome:
        from syndica.kernel import ConfigError
        from syndica.runner.batch import BatchTask, run_dataset

        path = Path(dataset)
        if not path.is_file():
            raise ConfigError(
                expected=f'{dataset} 是一份 jsonl 任务清单',
                received='这个路径不是文件',
                next_action='传 batch 的 dataset 参数指向每行一个 {"id","prompt"} 的文件',
            )

        def run_one(task: BatchTask) -> int:
            request = RunRequest(
                prompt=task.prompt, config=config, agent=task.agent, goal=task.goal
            )
            result = self._host.run_request(request)
            return result.exit_code if isinstance(result, CliOutcome) else 0

        body, code = run_dataset(path.read_text(encoding='utf-8'), run_one)
        return CliOutcome(body, code)

    def export(
        self,
        fmt: str,
        *,
        config: str,
        output: str,
        include_unverified: bool,
    ) -> CliOutcome:
        """导出用宿主的脱敏器：`.syndica.yaml` 的用户规则加此刻环境里的密钥值，与落盘同一套。"""
        from syndica.cli.handoff import artifact_root, rubric_root, session_root, trace_root
        from syndica.cli.redaction import host_secrets
        from syndica.runner.commands import export_workspace

        host = self._host.runtime_host()
        loaded = host.trusted_config(config)
        root = Path.cwd()
        body, code = export_workspace(
            root,
            (session_root(root), trace_root(root), rubric_root(root), artifact_root(root)),
            fmt,
            Path(output),
            redactor=host.redactor_for(loaded),
            secrets=host_secrets(os.environ),
            include_unverified=include_unverified,
        )
        return CliOutcome(body, code)


def entrypoint() -> None:
    io = CliIO(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    host = _LazyHost(io)
    code = EXIT_INTERRUPTED
    try:
        code = main(host=host, io=io, server=_ServerControl(io), training=_Training(host))
    except KeyboardInterrupt:
        # main 正在打印第一次 Ctrl-C 的说明时又按了一次：照样不打 traceback。
        code = EXIT_INTERRUPTED
    finally:
        try:
            host.close(interrupted=code == EXIT_INTERRUPTED)
        except KeyboardInterrupt:
            # 关 Runtime 时按的 Ctrl-C 不改变已经打印出去的结论，退出码照旧与它一致。
            pass
    # 只剩解释器退出：这时的 Ctrl-C 要么在退出途中打出一段 traceback，要么在解释器把处理换回默认
    # 之后连进程一起杀掉（退出码成了 -2），都改变不了已经打印出去的结论。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise SystemExit(code)


if __name__ == '__main__':
    entrypoint()
