"""组合根接 backend 时缺了 extra：三件套指向要装的那一个，不交出裸的 ImportError。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from syndica.__main__ import (
    _A2A_SPECS,  # pyright: ignore[reportPrivateUsage]
    _ACP_SPECS,  # pyright: ignore[reportPrivateUsage]
    _a2a_model,  # pyright: ignore[reportPrivateUsage]
    _acp_model,  # pyright: ignore[reportPrivateUsage]
    _open_mcp,  # pyright: ignore[reportPrivateUsage]
)
from syndica.cli.config import config_from_text
from syndica.cli.policy import dev_danger_options
from syndica.kernel import ConfigError, RuleRedactor

_MCP_CONFIG = """\
version: 1
agents:
  - name: Worker
    description: 干活
    system_prompt: 干活
    tools: [Read]
interop:
  mcp:
    servers:
      - name: files
        command: [/bin/echo, unused]
        roles: [Worker]
"""


def test_declared_mcp_servers_without_the_mcp_backend_name_the_mcp_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, 'syndica.backends.mcp.client', None)
    path = tmp_path / '.syndica.yaml'
    path.write_text(_MCP_CONFIG, encoding='utf-8')
    config = config_from_text(path, _MCP_CONFIG)
    judge, _table = dev_danger_options(tmp_path)
    with pytest.raises(ConfigError) as caught:
        asyncio.run(
            _open_mcp(
                config,
                {},
                cwd=str(tmp_path),
                redactor=RuleRedactor(),
                environ={},
                judge=judge,
                workspace=str(tmp_path),
            )
        )
    assert caught.value.next_action == "运行 pip install 'syndica[mcp]' 后重试"
    assert 'syndica.backends.mcp.client' in caught.value.received


def test_servers_no_role_uses_need_no_mcp_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, 'syndica.backends.mcp.client', None)
    text = _MCP_CONFIG.replace('roles: [Worker]', 'roles: []')
    path = tmp_path / '.syndica.yaml'
    path.write_text(text, encoding='utf-8')
    config = config_from_text(path, text)
    judge, _table = dev_danger_options(tmp_path)

    opened = asyncio.run(
        _open_mcp(
            config,
            {},
            cwd=str(tmp_path),
            redactor=RuleRedactor(),
            environ={},
            judge=judge,
            workspace=str(tmp_path),
        )
    )

    assert opened is None


def test_an_acp_role_without_the_acp_backend_names_the_acp_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, 'syndica.backends.acp', None)
    token = _ACP_SPECS.set({'gemini': (('/bin/echo', 'unused'), ())})
    try:
        with pytest.raises(ConfigError) as caught:
            _acp_model('acp:gemini')
    finally:
        _ACP_SPECS.reset(token)
    assert caught.value.next_action == "运行 pip install 'syndica[acp]' 后重试"
    assert 'syndica.backends.acp' in caught.value.received


def test_an_a2a_role_without_the_a2a_backend_names_the_a2a_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, 'syndica.backends.a2a', None)
    token = _A2A_SPECS.set({'remote': ('http://127.0.0.1:9/', None)})
    try:
        with pytest.raises(ConfigError) as caught:
            _a2a_model('a2a:remote')
    finally:
        _A2A_SPECS.reset(token)
    assert caught.value.next_action == "运行 pip install 'syndica[a2a]' 后重试"
    assert 'syndica.backends.a2a' in caught.value.received
