"""TRL 后端：没装 extra 时导入本模块不得拉起 torch，dry_run 指出该装哪个 extra。"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
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


def test_importing_the_trl_backend_does_not_load_torch() -> None:
    probe = (
        'import sys, syndica.backends.trl, syndica.runner.training, syndica.runner.assemble\n'
        'print(sorted(name for name in ("torch", "trl", "datasets", "transformers") '
        'if name in sys.modules))\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '[]'


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

    call = {
        'id': 'c1',
        'type': 'function',
        'function': {'name': 'Write', 'arguments': '{"path": "a b c d e f g h"}'},
    }
    plain = [{'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'world'}]
    with_call = [
        {'role': 'user', 'content': 'hello'},
        {'role': 'assistant', 'content': 'world', 'tool_calls': [call]},
    ]
    dataset = tmp_path / 'messages.jsonl'
    dataset.write_text(
        json.dumps({'messages': plain}) + '\n' + json.dumps({'messages': with_call}) + '\n',
        encoding='utf-8',
    )
    result = dry_run(dataset)
    assert result['rows'] == 2
    assert result['max_steps'] == 0
    assert result['columns'] == ['messages']
    prepared = result['prepared_columns']
    assert isinstance(prepared, list)
    assert 'input_ids' in prepared
    counts = result['token_counts']
    assert isinstance(counts, list)
    # 工具调用经模板进了分词：同一段对话多了它，token 就多。
    assert counts[1] > counts[0]
