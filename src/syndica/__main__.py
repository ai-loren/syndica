"""`python -m syndica` entry point."""

from __future__ import annotations

import os
import re
import signal
import sys
from collections.abc import Sequence
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
    from collections.abc import Mapping

    from syndica.cli.host import RuntimeCliHost
    from syndica.cli.trace_commands import Replayed
    from syndica.kernel import LLM, ScenarioDocument, TraceRecorder, TraceStore
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


class _LazyHost:
    def __init__(self, io: CliIO) -> None:
        self._io = io
        self._host: RuntimeCliHost | None = None

    def _get(self) -> RuntimeCliHost:
        if self._host is None:
            from syndica.cli.host import RuntimeCliHost
            from syndica.cli.modules import installed_entry_points
            from syndica.cli.trust import real_trust_gate

            self._host = RuntimeCliHost(
                io=self._io,
                model_factory=_configured_model,
                workspace_root=Path.cwd(),
                replayer=_replay_document,
                exporter=_export_trace,
                entry_points=installed_entry_points,
                trust=real_trust_gate(),
                prices=_default_prices(),
            )
        return self._host

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


def entrypoint() -> None:
    io = CliIO(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    host = _LazyHost(io)
    code = EXIT_INTERRUPTED
    try:
        code = main(host=host, io=io)
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
