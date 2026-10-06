"""前端产物随 Python 包发布：不过期、不带内联脚本、进 wheel。这些都不需要 Node。"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / 'src' / 'syndica' / 'server' / 'static'
_WHEEL_BUDGET = 3_000_000


def _build_info() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        'web_build_info', _ROOT / 'scripts' / 'web_build_info.py'
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_bundle_was_built_from_the_current_sources_and_contract():
    recorded = json.loads((_STATIC / 'build-info.json').read_text(encoding='utf-8'))
    assert recorded == _build_info().current(), (
        '前端源码、构建配置或 OpenAPI 快照改了却没重新构建：运行 scripts/check_web.sh 并提交产物'
    )


def test_the_inputs_cover_the_contract_and_every_frontend_source():
    module = _build_info()
    names = {path.relative_to(_ROOT).as_posix() for path in module.input_files()}
    assert 'docs/reference/openapi.json' in names
    assert 'web/src/App.tsx' in names
    assert 'web/src/generated/openapi.ts' in names
    assert {
        path.relative_to(_ROOT).as_posix()
        for path in (_ROOT / 'web' / 'src').rglob('*')
        if path.is_file()
    } <= names


def test_the_page_loads_only_same_origin_files_and_no_inline_script():
    page = (_STATIC / 'index.html').read_text(encoding='utf-8')
    scripts = re.findall(r'<script\b[^>]*>(.*?)</script>', page, flags=re.S)
    assert scripts and all(body.strip() == '' for body in scripts)
    assert re.findall(r'src="([^"]+)"', page) == ['/assets/app.js']
    assert 'http' not in page


def test_unverified_has_its_own_label_and_never_the_passing_tone():
    source = (_ROOT / 'web' / 'src' / 'verdict.ts').read_text(encoding='utf-8')
    assert re.search(r"unverified: \{ label: '未验收', tone: 'unknown' \}", source)
    assert re.search(r"pass: \{ label: '通过', tone: 'ok' \}", source)


@pytest.mark.skipif(shutil.which('uv') is None, reason='builds the wheel with uv')
def test_the_wheel_ships_the_bundle_within_its_size_budget(tmp_path: Path):
    subprocess.run(
        ['uv', 'build', '--wheel', '--out-dir', str(tmp_path), '--quiet'],
        cwd=_ROOT,
        check=True,
        timeout=120,
    )
    [wheel] = tmp_path.glob('*.whl')
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert {
        'syndica/server/static/index.html',
        'syndica/server/static/assets/app.js',
        'syndica/server/static/build-info.json',
    } <= names
    assert not any(name.startswith(('web/', 'syndica/web/')) for name in names)
    assert wheel.stat().st_size < _WHEEL_BUDGET
