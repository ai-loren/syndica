"""`python -m syndica` entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from syndica.cli.main import CliIO, InspectRequest, RunRequest, main

if TYPE_CHECKING:
    from syndica.cli.host import RuntimeCliHost
    from syndica.kernel import LLM


def _openai_model(name: str) -> LLM:
    from syndica.backends.llm_openai import OpenAICompatible
    from syndica.kernel import ConfigError, LLMConfig

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
    client_args = {'base_url': base_url}
    api_key = os.environ.get('OPENAI_API_KEY')
    if api_key:
        client_args['api_key'] = api_key
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
                model_factory=_openai_model,
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
