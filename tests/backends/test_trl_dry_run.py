"""TRL 后端：没装 extra 时导入本模块不得拉起 torch，dry_run 指出该装哪个 extra。"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from syndica.kernel import ConfigError

_MODULE = Path(__file__).parents[2] / 'src' / 'syndica' / 'backends' / 'trl' / 'dry_run.py'


def test_the_trl_module_does_not_import_torch_at_import_time() -> None:
    tree = ast.parse(_MODULE.read_text(encoding='utf-8'))
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split('.')[0])
    assert 'trl' not in imported
    assert 'torch' not in imported
    assert 'datasets' not in imported
    importlib.import_module('syndica.backends.trl.dry_run')


def test_dry_run_without_trl_names_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module('syndica.backends.trl.dry_run')

    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(module.importlib, 'import_module', missing)
    with pytest.raises(ConfigError, match='syndica\\[trl\\]'):
        module.dry_run(tmp_path / 'messages.jsonl')


def test_the_trainer_tokenizes_an_sft_jsonl_without_downloading_a_model(tmp_path: Path) -> None:
    pytest.importorskip('trl')
    from syndica.backends.trl.dry_run import dry_run

    dataset = tmp_path / 'messages.jsonl'
    dataset.write_text(
        '{"messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"world"}]}\n',
        encoding='utf-8',
    )
    result = dry_run(dataset)
    assert result['rows'] == 1
    assert result['max_steps'] == 0
    assert result['columns'] == ['messages']
    prepared = result['prepared_columns']
    assert isinstance(prepared, list)
    assert 'input_ids' in prepared
