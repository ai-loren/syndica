"""`python -m syndica` entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from syndica.cli.main import CliIO, InspectRequest, RunRequest, main

if TYPE_CHECKING:
    from syndica.cli.host import RuntimeCliHost
    from syndica.kernel import LLM

_OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
_OPENROUTER_DEFAULT_MODEL = 'openrouter/free'


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
            received=f'收到模型 {value!r}',
            next_action='改成 openrouter/free，或从 OpenRouter 免费模型列表选择一个 :free 模型',
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


def _configured_model(name: str) -> LLM:
    from syndica.backends.llm_openai import OpenAICompatible
    from syndica.kernel import ConfigError, LLMConfig

    if 'OPENROUTER_API_KEY' in os.environ or 'OPENROUTER_MODEL' in os.environ:
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
            source='OPENROUTER_MODEL',
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

    base_url = os.environ.get('OPENAI_BASE_URL')
    model = os.environ.get('OPENAI_MODEL') if name == 'default' else name
    if not base_url or not base_url.strip():
        raise ConfigError(
            expected='OPENAI_BASE_URL 是非空的 OpenAI-compatible API 根 URL',
            received=f'收到 OPENAI_BASE_URL={base_url!r}',
            next_action='设置 OPENAI_BASE_URL，例如 https://host/v1，再重试 run/chat/doctor',
        )
    if not model or not model.strip():
        raise ConfigError(
            expected='默认模型由非空 OPENAI_MODEL 指定',
            received=f'收到 OPENAI_MODEL={model!r}',
            next_action='设置 OPENAI_MODEL；清单显式 model 字段不使用这个默认值',
        )
    if _is_openrouter_url(base_url):
        model = _free_openrouter_model(model, source='OPENAI_MODEL')
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


class _LazyHost:
    def __init__(self, io: CliIO) -> None:
        self._io = io
        self._host: RuntimeCliHost | None = None

    def _get(self) -> RuntimeCliHost:
        if self._host is None:
            from syndica.cli.host import RuntimeCliHost

            self._host = RuntimeCliHost(
                io=self._io,
                model_factory=_configured_model,
                workspace_root=Path.cwd(),
            )
        return self._host

    def run_request(self, request: RunRequest) -> str:
        return self._get().run_request(request)

    def inspect(self, request: InspectRequest) -> str:
        return self._get().inspect(request)

    def doctor(self, config: str) -> str:
        return self._get().doctor(config)

    def open_chat(self, config: str) -> str:
        return self._get().open_chat(config)

    def active_agents(self) -> str:
        return self._get().active_agents()

    def close(self) -> None:
        if self._host is not None:
            self._host.close()


def entrypoint() -> None:
    io = CliIO(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    host = _LazyHost(io)
    try:
        code = main(host=host, io=io)
    finally:
        host.close()
    raise SystemExit(code)


if __name__ == '__main__':
    entrypoint()
