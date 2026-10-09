"""TRL 后端：没装 extra 时导入本模块不得拉起 torch，dry-run 指出该装哪个 extra；装了时 TRL 读得进
`write_export` 写出的 SFT 与偏好文件。"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from syndica.kernel import ConfigError
from syndica.runner.training import Attempt, Call, Message, write_export

_MODULE = Path(__file__).parents[2] / 'src' / 'syndica' / 'backends' / 'trl' / 'dry_run.py'
_CALL = Call('call-1', 'Write', '{"path": "a b c d e f g h"}')


def _attempt(ident: str, verdict: str, calls: tuple[Call, ...] = ()) -> Attempt:
    return Attempt(
        id=ident,
        goal_id='goal-planets',
        rubric_id='rubric-1',
        messages=(
            Message('user', 'write planets.md'),
            Message('assistant', 'wrote the note', tool_calls=calls),
        ),
        verdict=verdict,
    )


def _export(tmp_path: Path, fmt: str, attempts: Sequence[Attempt]) -> Path:
    out = tmp_path / fmt
    write_export(attempts, fmt, out)
    return out / {'sft': 'messages.jsonl', 'preference': 'preference.jsonl'}[fmt]


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


@pytest.mark.parametrize('function', ['dry_run', 'dry_run_preference'])
def test_dry_run_without_trl_names_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, function: str
) -> None:
    module = importlib.import_module('syndica.backends.trl.dry_run')

    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(module.importlib, 'import_module', missing)
    with pytest.raises(ConfigError, match='syndica\\[trl\\]') as raised:
        getattr(module, function)(tmp_path / 'data.jsonl')
    assert function in raised.value.next_action


@pytest.mark.parametrize(
    ('function', 'fmt'), [('dry_run', 'sft'), ('dry_run_preference', 'preference')]
)
def test_a_path_that_is_not_an_export_file_names_the_export_command(
    tmp_path: Path, function: str, fmt: str
) -> None:
    pytest.importorskip('trl')
    module = importlib.import_module('syndica.backends.trl.dry_run')
    with pytest.raises(ConfigError) as raised:
        getattr(module, function)(tmp_path)
    assert f'syndica export {fmt}' in raised.value.next_action


@pytest.mark.parametrize(
    ('function', 'fmt', 'attempts', 'anchor'),
    [
        ('dry_run', 'sft', [_attempt('only', 'unverified')], '--include-unverified'),
        (
            'dry_run_preference',
            'preference',
            [_attempt('first', 'pass'), _attempt('second', 'pass')],
            'preference_pairs',
        ),
    ],
    ids=['sft-all-unverified', 'preference-no-pair'],
)
def test_an_export_with_no_rows_says_why_before_trl_sees_it(
    tmp_path: Path, function: str, fmt: str, attempts: list[Attempt], anchor: str
) -> None:
    pytest.importorskip('trl')
    module = importlib.import_module('syndica.backends.trl.dry_run')
    dataset = _export(tmp_path, fmt, attempts)
    assert dataset.read_text(encoding='utf-8') == ''
    with pytest.raises(ConfigError) as raised:
        getattr(module, function)(dataset)
    assert anchor in raised.value.next_action


def test_trl_labels_an_exported_sft_file_and_its_tool_calls_reach_the_tokenizer(
    tmp_path: Path,
) -> None:
    pytest.importorskip('trl')
    from syndica.backends.trl import dry_run

    dataset = _export(
        tmp_path, 'sft', [_attempt('plain', 'pass'), _attempt('with-call', 'pass', (_CALL,))]
    )
    lines = dataset.read_text(encoding='utf-8').splitlines()
    assert [json.loads(line)['id'] for line in lines] == ['plain', 'with-call']
    result = dry_run(dataset)
    assert result['rows'] == 2
    assert result['max_steps'] == 0
    columns = result['columns']
    assert isinstance(columns, list) and 'messages' in columns
    prepared = result['prepared_columns']
    assert isinstance(prepared, list)
    assert {'input_ids', 'labels'} <= set(prepared)
    counts = result['token_counts']
    assert isinstance(counts, list) and len(counts) == 2
    assert counts[1] > counts[0]


def test_dpo_tokenizes_an_exported_preference_file_and_its_tool_calls_reach_the_tokenizer(
    tmp_path: Path,
) -> None:
    pytest.importorskip('trl')
    from syndica.backends.trl import dry_run_preference

    attempts = [_attempt('chosen', 'pass', (_CALL,)), _attempt('rejected', 'block')]
    dataset = _export(tmp_path, 'preference', attempts)
    [row] = [json.loads(line) for line in dataset.read_text(encoding='utf-8').splitlines()]
    assert (row['chosen_id'], row['rejected_id']) == ('chosen', 'rejected')
    result = dry_run_preference(dataset)
    assert result['rows'] == 1
    assert result['max_steps'] == 0
    columns = result['columns']
    assert isinstance(columns, list) and {'prompt', 'chosen', 'rejected'} <= set(columns)
    prepared = result['prepared_columns']
    assert isinstance(prepared, list)
    assert {'prompt_ids', 'chosen_ids', 'rejected_ids'} <= set(prepared)
    prompt = result['prompt_token_counts']
    chosen = result['chosen_token_counts']
    rejected = result['rejected_token_counts']
    assert isinstance(prompt, list) and isinstance(chosen, list) and isinstance(rejected, list)
    assert len(prompt) == len(chosen) == len(rejected) == 1
    assert prompt[0] > 0
    assert rejected[0] > 0
    assert chosen[0] > rejected[0]
