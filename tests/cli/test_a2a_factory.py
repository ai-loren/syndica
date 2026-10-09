"""`model: a2a:<名字>` 由组合根解析成还没连上的 A2AAgentModel。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.__main__ import _a2a_model, _remember_a2a
from syndica.cli.config import CliConfig, load_config
from syndica.kernel import ConfigError


def _config(tmp_path: Path) -> CliConfig:
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('remote', encoding='utf-8')
    path = tmp_path / '.syndica.yaml'
    path.write_text(
        """
version: 1
agents:
  - name: Worker
    description: remote worker
    system_prompt: prompt.md
    model: a2a:remote
    tools: [Read]
interop:
  a2a:
    agents:
      - name: remote
        url: http://127.0.0.1:9/a2a/
""",
        encoding='utf-8',
    )
    return load_config(path)


def test_an_a2a_model_name_points_at_the_declared_url(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_a2a(config, tmp_path, {})
    model = _a2a_model('a2a:remote')
    assert model.config.provider == 'a2a'
    assert model.config.model == 'remote'


def test_an_unknown_a2a_name_names_the_declared_agents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_a2a(config, tmp_path, {})
    with pytest.raises(ConfigError, match='interop.a2a.agents'):
        _a2a_model('a2a:missing')
