"""`model: acp:<名字>` 由组合根解析成还没拉起的 AcpAgentModel。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.__main__ import _acp_model, _remember_acp
from syndica.cli.config import CliConfig, load_config
from syndica.kernel import ConfigError


def _config(tmp_path: Path) -> CliConfig:
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('hosted', encoding='utf-8')
    path = tmp_path / '.syndica.yaml'
    path.write_text(
        """
version: 1
agents:
  - name: Worker
    description: hosted worker
    system_prompt: prompt.md
    model: acp:gemini
    tools: [Read]
interop:
  acp:
    agents:
      - name: gemini
        command: ["/bin/echo", "acp"]
        pass_env: [NO_COLOR]
""",
        encoding='utf-8',
    )
    return load_config(path)


def test_an_acp_model_name_builds_a_hosted_agent_without_starting_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_acp(config, tmp_path, {})
    model = _acp_model('acp:gemini')
    assert model.config.provider == 'acp'
    assert model.config.model == 'gemini'


def test_an_unknown_acp_name_names_the_declared_agents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_acp(config, tmp_path, {})
    with pytest.raises(ConfigError, match='interop.acp.agents'):
        _acp_model('acp:missing')
