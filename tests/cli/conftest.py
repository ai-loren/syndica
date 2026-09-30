"""CLI 测试共用的装置。"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_agent_scratch_left_behind(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """每条用例都有自己的系统临时目录，结束时里面不能留下 Agent 的临时 git / gh 配置。

    这样每一条走失败路径的 host 用例（模型报错、根目录不存在、人类输入读失败）都顺带证明了收尾。
    """
    scratch_parent = tmp_path_factory.mktemp('system-tmp')
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch_parent))
    yield scratch_parent
    assert sorted(path.name for path in scratch_parent.glob('syndica-agent-*')) == []
