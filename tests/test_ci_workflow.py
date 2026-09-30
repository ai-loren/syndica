"""CI 跑的门禁与 README「Development」一节逐字一致，而且一道都不少。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).parents[1]
WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
README = REPO_ROOT / 'README.md'


def _workflow() -> dict[object, Any]:
    return cast(dict[object, Any], yaml.safe_load(WORKFLOW.read_text(encoding='utf-8')))


def _steps() -> list[dict[str, Any]]:
    jobs = cast(dict[str, Any], _workflow()['jobs'])
    [job] = jobs.values()
    return cast(list[dict[str, Any]], job['steps'])


def _run_commands() -> list[str]:
    return [str(step['run']).strip() for step in _steps() if 'run' in step]


def _readme_gates() -> list[str]:
    text = README.read_text(encoding='utf-8')
    match = re.search(r'Run the normal local gates:\s*```bash\n(.*?)```', text, re.DOTALL)
    assert match is not None, (
        "README 的 Development 一节里找不到 'Run the normal local gates' 代码块"
    )
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def test_ci_runs_exactly_the_readme_gates_in_order_after_a_locked_sync() -> None:
    assert _run_commands() == ['uv sync --locked --extra openai --extra dev', *_readme_gates()]


def test_the_readme_lists_the_five_gates() -> None:
    assert _readme_gates() == [
        'uv run ruff format --check .',
        'uv run ruff check .',
        'uv run pyright',
        'uv run python scripts/check_deps.py',
        'uv run pytest -q',
    ]


def test_ci_runs_on_pushes_to_main_and_on_pull_requests() -> None:
    workflow = _workflow()
    # YAML 1.1 把裸键 on 解析成布尔 True。
    triggers = cast(dict[str, Any], workflow.get('on', workflow.get(True)))

    assert list(triggers) == ['push', 'pull_request']
    assert triggers['push'] == {'branches': ['main']}


def test_ci_pins_python_3_11_and_an_exact_uv_version() -> None:
    [setup] = [
        step for step in _steps() if str(step.get('uses', '')).startswith('astral-sh/setup-uv@')
    ]
    options = cast(dict[str, str], setup['with'])

    assert options['python-version'] == '3.11'
    assert re.fullmatch(r'\d+\.\d+\.\d+', options['version'])
