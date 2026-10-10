"""CI 的矩阵、各个 job 与本地复刻（`scripts/local_ci.sh`）逐条对得上，门禁与 README「Development」
一节逐字一致，而且一道都不少。"""

from __future__ import annotations

import itertools
import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).parents[1]
WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
README = REPO_ROOT / 'README.md'
LOCAL_CI = REPO_ROOT / 'scripts' / 'local_ci.sh'
LOCAL_CELL = REPO_ROOT / 'scripts' / 'local_ci_cell.sh'
AUDIT = REPO_ROOT / 'scripts' / 'audit_deps.sh'
_CELL_SYSTEMS = {'macos': 'macos-latest', 'linux': 'ubuntu-latest'}


def _workflow() -> dict[object, Any]:
    return cast(dict[object, Any], yaml.safe_load(WORKFLOW.read_text(encoding='utf-8')))


def _jobs() -> dict[str, dict[str, Any]]:
    return cast(dict[str, dict[str, Any]], _workflow()['jobs'])


def _steps(job: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _jobs()[job]['steps'])


def _run_commands(job: str) -> list[str]:
    return [str(step['run']).strip() for step in _steps(job) if 'run' in step]


def _all_steps() -> list[dict[str, Any]]:
    return [step for job in _jobs() for step in _steps(job)]


def _uses(prefix: str) -> list[dict[str, Any]]:
    return [step for step in _all_steps() if str(step.get('uses', '')).startswith(prefix)]


def _cells() -> list[dict[str, object]]:
    """按 GitHub 的规则展开 gates 矩阵：include 里的一项并进所有原始取值都对得上的格子，一个都并不进
    才另起一格。"""
    matrix = dict(cast(dict[str, Any], _jobs()['gates']['strategy']['matrix']))
    includes = cast(list[dict[str, object]], matrix.pop('include', []))
    keys = list(matrix)
    cells = [dict(zip(keys, values, strict=True)) for values in itertools.product(*matrix.values())]
    for extra in includes:
        matching = [
            cell for cell in cells if all(cell[k] == v for k, v in extra.items() if k in keys)
        ]
        for cell in matching:
            cell.update(extra)
        if not matching:
            cells.append(dict(extra))
    return cells


def _readme_gates() -> list[str]:
    text = README.read_text(encoding='utf-8')
    match = re.search(r'Run the normal local gates:\s*```bash\n(.*?)```', text, re.DOTALL)
    assert match is not None, (
        "README 的 Development 一节里找不到 'Run the normal local gates' 代码块"
    )
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def _local_steps(job: str) -> list[tuple[str, str]]:
    text = LOCAL_CELL.read_text(encoding='utf-8')
    branch = re.search(rf'^{job}\)\n(.*?)^\s*;;', text, re.MULTILINE | re.DOTALL)
    assert branch is not None, f'scripts/local_ci_cell.sh 里没有 {job}) 这一支'
    steps: list[tuple[str, str]] = []
    for line in branch.group(1).splitlines():
        if inside := re.fullmatch(r'\s*gate_in (\S+) (.+)', line):
            steps.append((inside.group(1), inside.group(2)))
        elif plain := re.fullmatch(r'\s*gate (.+)', line):
            steps.append(('.', plain.group(1)))
    return steps


def _workflow_steps(job: str) -> list[tuple[str, str]]:
    return [
        (str(step.get('working-directory', '.')), str(step['run']).strip())
        for step in _steps(job)
        if 'run' in step
    ]


def _local_matrix() -> list[dict[str, object]]:
    match = re.search(r"^MATRIX='\n(.*?)^'", LOCAL_CI.read_text(encoding='utf-8'), re.M | re.S)
    assert match is not None, "scripts/local_ci.sh 里没有 MATRIX='...' 表"
    rows = [line.split() for line in match.group(1).splitlines() if line.strip()]
    return [
        {
            'os': _CELL_SYSTEMS[system],
            'python': python,
            'trl': trl == 'yes',
            'pyright': pyright == 'yes',
        }
        for _, system, python, trl, pyright in rows
    ]


def test_ci_runs_on_pushes_to_main_and_on_pull_requests() -> None:
    workflow = _workflow()
    # YAML 1.1 把裸键 on 解析成布尔 True。
    triggers = cast(dict[str, Any], workflow.get('on', workflow.get(True)))

    assert list(triggers) == ['push', 'pull_request']
    assert triggers['push'] == {'branches': ['main']}


def test_the_jobs_are_the_gate_matrix_web_package_and_security() -> None:
    assert list(_jobs()) == ['gates', 'web', 'package', 'security']


def test_the_gate_job_runs_exactly_the_readme_gates_in_order_after_a_locked_sync() -> None:
    assert _run_commands('gates') == [
        'uv sync --locked --all-extras --no-extra trl',
        'uv sync --locked --all-extras',
        *_readme_gates(),
    ]


def test_the_readme_lists_the_gates() -> None:
    assert _readme_gates() == [
        'uv run ruff format --check .',
        'uv run ruff check .',
        'uv run pyright',
        'uv run python scripts/check_deps.py',
        'uv run python scripts/check_links.py',
        'uv run pytest -q',
    ]


def test_the_gate_matrix_is_ubuntu_and_macos_by_python_3_11_3_12_3_13() -> None:
    matrix = cast(dict[str, Any], _jobs()['gates']['strategy']['matrix'])

    assert matrix['os'] == ['ubuntu-latest', 'macos-latest']
    assert matrix['python'] == ['3.11', '3.12', '3.13']
    assert sorted((str(cell['os']), str(cell['python'])) for cell in _cells()) == sorted(
        itertools.product(['ubuntu-latest', 'macos-latest'], ['3.11', '3.12', '3.13'])
    )


def test_no_job_runs_on_windows() -> None:
    systems = [str(job['runs-on']) for job in _jobs().values()]
    systems += [str(cell['os']) for cell in _cells()]

    assert [system for system in systems if 'windows' in system.lower()] == []


def test_the_gate_job_runs_on_the_matrix_os_with_the_matrix_python() -> None:
    [setup] = [step for step in _steps('gates') if 'setup-uv' in str(step.get('uses', ''))]

    assert _jobs()['gates']['runs-on'] == '${{ matrix.os }}'
    assert cast(dict[str, str], setup['with'])['python-version'] == '${{ matrix.python }}'


def test_one_failing_cell_does_not_cancel_the_others() -> None:
    assert _jobs()['gates']['strategy']['fail-fast'] is False


def test_torch_is_installed_in_exactly_one_cell_and_it_is_a_macos_cell() -> None:
    with_torch = [(cell['os'], cell['python']) for cell in _cells() if cell.get('trl')]
    conditions = {
        str(step['run']).strip(): step.get('if') for step in _steps('gates') if 'run' in step
    }

    assert with_torch == [('macos-latest', '3.13')]
    assert conditions['uv sync --locked --all-extras --no-extra trl'] == '${{ !matrix.trl }}'
    assert conditions['uv sync --locked --all-extras'] == '${{ matrix.trl }}'


def test_pyright_runs_in_exactly_one_cell_on_the_python_version_pyproject_pins() -> None:
    pinned = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    with_pyright = [cell['python'] for cell in _cells() if cell.get('pyright')]
    conditional = {str(step['run']).strip(): step['if'] for step in _steps('gates') if 'if' in step}

    assert with_pyright == [pinned['tool']['pyright']['pythonVersion']]
    assert conditional['uv run pyright'] == '${{ matrix.pyright }}'
    assert set(conditional) == {
        'uv run pyright',
        'uv sync --locked --all-extras',
        'uv sync --locked --all-extras --no-extra trl',
    }


def test_every_python_install_is_locked_and_every_npm_install_is_clean() -> None:
    commands = [command for job in _jobs() for command in _run_commands(job)]
    audit = AUDIT.read_text(encoding='utf-8')

    assert [c for c in commands if 'uv sync' in c and '--locked' not in c] == []
    assert [c for c in commands if re.search(r'\bnpm (install|i|update)\b', c)] == []
    assert 'npm ci --no-audit --no-fund' in _run_commands('web')
    assert re.search(r'^uv export [^\n]*--locked', audit, re.MULTILINE)


def test_every_job_has_a_timeout() -> None:
    timeouts = {name: job.get('timeout-minutes') for name, job in _jobs().items()}

    assert [name for name, minutes in timeouts.items() if not isinstance(minutes, int)] == []


def test_the_token_is_read_only_in_every_job() -> None:
    assert _workflow()['permissions'] == {'contents': 'read'}
    assert [name for name, job in _jobs().items() if 'permissions' in job] == []


def test_checkout_does_not_leave_the_token_in_the_working_tree() -> None:
    checkouts = _uses('actions/checkout@')

    assert len(checkouts) == len(_jobs())
    assert [step.get('with', {}).get('persist-credentials') for step in checkouts] == [False] * len(
        checkouts
    )


def test_a_newer_push_cancels_the_running_ci_for_the_same_ref() -> None:
    assert _workflow()['concurrency'] == {
        'group': 'ci-${{ github.ref }}',
        'cancel-in-progress': True,
    }


def test_every_action_is_pinned_to_a_full_release_tag() -> None:
    # setup-uv 从 v8 起不再发布 v10 这种浮动大版本标签；写成 @v10，CI 在 Set up job 就解析失败。
    uses = [str(step['uses']) for step in _all_steps() if 'uses' in step]

    assert uses
    assert [u for u in uses if not re.fullmatch(r'[\w.-]+/[\w.-]+@v\d+\.\d+\.\d+', u)] == []


def test_every_job_pins_the_same_exact_uv_version_as_local_ci() -> None:
    versions = {
        cast(dict[str, str], step['with'])['version'] for step in _uses('astral-sh/setup-uv@')
    }
    local = re.search(r'^UV_VERSION=(\S+)$', LOCAL_CI.read_text(encoding='utf-8'), re.MULTILINE)

    assert len(versions) == 1
    [version] = versions
    assert re.fullmatch(r'\d+\.\d+\.\d+', version)
    assert local is not None and local.group(1) == version


def test_node_comes_from_web_package_json_and_is_the_major_the_bundle_is_built_with() -> None:
    nodes = [cast(dict[str, str], step['with']) for step in _uses('actions/setup-node@')]
    engines = json.loads((REPO_ROOT / 'web' / 'package.json').read_text(encoding='utf-8'))
    build_info = (REPO_ROOT / 'scripts' / 'web_build_info.py').read_text(encoding='utf-8')
    major = re.search(r'^NODE_MAJOR = (\d+)$', build_info, re.MULTILINE)

    assert [options.get('node-version-file') for options in nodes] == ['web/package.json'] * 2
    assert [options.get('node-version') for options in nodes] == [None, None]
    assert major is not None
    assert engines['engines']['node'] == f'>={major.group(1)} <{int(major.group(1)) + 1}'


def test_the_web_job_installs_chromium_before_the_e2e_and_then_checks_the_bundle_reproduced() -> (
    None
):
    assert _workflow_steps('web') == [
        ('.', 'uv sync --locked --all-extras --no-extra trl'),
        ('web', 'npm ci --no-audit --no-fund'),
        ('web', 'npx playwright install --with-deps chromium'),
        ('.', 'scripts/check_e2e.sh'),
        ('.', 'scripts/check_web_reproduced.sh'),
    ]


def test_the_package_job_builds_and_checks_the_distributions_strictly() -> None:
    assert _run_commands('package') == [
        'uv build',
        'uv tool run twine==7.0.0 check --strict dist/*',
    ]


def test_the_security_job_audits_every_locked_python_package_and_the_web_lockfile() -> None:
    audit = AUDIT.read_text(encoding='utf-8')

    assert _run_commands('security') == ['scripts/audit_deps.sh']
    assert re.search(r'^uv export [^\n]*--all-extras', audit, re.MULTILINE)
    assert 'pip-audit==' in audit
    assert 'npm audit --audit-level=high' in audit


def test_every_script_the_workflow_runs_directly_is_executable() -> None:
    scripts = {
        c.split()[0] for job in _jobs() for c in _run_commands(job) if c.startswith('scripts/')
    }

    assert scripts == {
        'scripts/audit_deps.sh',
        'scripts/check_e2e.sh',
        'scripts/check_web_reproduced.sh',
    }
    assert [s for s in sorted(scripts) if not (REPO_ROOT / s).stat().st_mode & 0o111] == []


def test_local_ci_runs_the_same_steps_as_every_workflow_job() -> None:
    mismatched = {
        job: (_local_steps(job), _workflow_steps(job))
        for job in _jobs()
        if _local_steps(job) != _workflow_steps(job)
    }

    assert mismatched == {}


def test_local_ci_mirrors_the_gate_matrix_cell_for_cell() -> None:
    workflow = [
        {
            'os': cell['os'],
            'python': cell['python'],
            'trl': bool(cell.get('trl')),
            'pyright': bool(cell.get('pyright')),
        }
        for cell in _cells()
    ]

    def key(cell: dict[str, object]) -> tuple[str, str]:
        return str(cell['os']), str(cell['python'])

    assert sorted(_local_matrix(), key=key) == sorted(workflow, key=key)
