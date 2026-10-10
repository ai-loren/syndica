"""发布要的仓库文件都在，SECURITY 写着两条硬声明，pyproject 的元数据齐全且不声明 Windows，
版本号是规范的 PEP 440 且与 `syndica --version` 打印的一致。"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).parents[1]
CHECK_DIST = REPO_ROOT / 'scripts' / 'check_dist.py'
RELEASE_FILES = (
    'CHANGELOG.md',
    'CODE_OF_CONDUCT.md',
    'CONTRIBUTING.md',
    'LICENSE',
    'README.md',
    'RELEASING.md',
    'SECURITY.md',
    'docs/user/versioning.md',
    '.github/ISSUE_TEMPLATE/bug_report.yml',
    '.github/ISSUE_TEMPLATE/config.yml',
    '.github/ISSUE_TEMPLATE/feature_request.yml',
    '.github/PULL_REQUEST_TEMPLATE.md',
)
SECURITY_STATEMENTS = (
    'Syndica provides no multi-tenant isolation.',
    'Third-party code that runs in-process, such as Runtime Modules and backends, cannot be '
    'sandboxed.',
)
REQUIRED_CLASSIFIERS = (
    'Development Status :: 4 - Beta',
    'Framework :: AsyncIO',
    'Intended Audience :: Developers',
    'Operating System :: MacOS',
    'Operating System :: POSIX :: Linux',
    'Programming Language :: Python :: 3',
    'Programming Language :: Python :: 3.11',
    'Programming Language :: Python :: 3.12',
    'Programming Language :: Python :: 3.13',
    'Typing :: Typed',
)
EXTRAS = ('openai', 'server', 'otel', 'mcp', 'acp', 'a2a', 'trl')
URLS = ('Homepage', 'Repository', 'Issues', 'Changelog', 'Documentation')


def _project() -> dict[str, Any]:
    data = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    return cast(dict[str, Any], data['project'])


def _flat(text: str) -> str:
    return ' '.join(text.split())


@pytest.mark.parametrize('relative', RELEASE_FILES)
def test_release_file_exists_and_is_not_blank(relative: str) -> None:
    path = REPO_ROOT / relative

    assert path.is_file(), f'缺 {relative}：发布清单要它，补上这个文件'
    assert path.read_text(encoding='utf-8').strip(), f'{relative} 是空的'


@pytest.mark.parametrize('statement', SECURITY_STATEMENTS)
def test_security_policy_states_each_mandatory_limit_verbatim(statement: str) -> None:
    text = _flat((REPO_ROOT / 'SECURITY.md').read_text(encoding='utf-8'))

    assert statement in text


def test_pyproject_declares_the_release_metadata() -> None:
    project = _project()

    assert project['name'] == 'syndica'
    assert project['requires-python'] == '>=3.11'
    assert project['license'] == 'MIT'
    assert project['license-files'] == ['LICENSE']
    assert project['readme'] == 'README.md'
    assert project['description'].strip()
    assert project['keywords']
    assert project['scripts'] == {'syndica': 'syndica.__main__:entrypoint'}


@pytest.mark.parametrize('extra', EXTRAS)
def test_each_extra_is_declared_with_dependencies(extra: str) -> None:
    assert _project()['optional-dependencies'].get(extra)


# backend 包 -> 带它的 extra。直接 import httpx 的 backend 用的是 0.x 的接口。
HTTPX_BACKENDS = {'llm_openai': 'openai', 'otel_otlp': 'otel', 'a2a': 'a2a'}


def _imports_httpx(package: Path) -> bool:
    return any(
        line.strip() in {'import httpx'} or line.strip().startswith('from httpx ')
        for path in package.rglob('*.py')
        for line in path.read_text(encoding='utf-8').splitlines()
    )


def test_every_backend_that_imports_httpx_is_mapped_to_its_extra() -> None:
    backends = REPO_ROOT / 'src' / 'syndica' / 'backends'
    importing = {path.name for path in backends.iterdir() if path.is_dir() and _imports_httpx(path)}

    assert importing == set(HTTPX_BACKENDS), (
        f'期望: 直接 import httpx 的 backend 都在 HTTPX_BACKENDS 里。实际: {sorted(importing)}。'
        ' 下一步: 把新 backend 和带它的 extra 加进映射，并给那个 extra 的 httpx 封顶 <1。'
    )


@pytest.mark.parametrize('extra', sorted({*HTTPX_BACKENDS.values(), 'dev'}))
def test_an_extra_whose_code_imports_httpx_caps_it_below_1(extra: str) -> None:
    requirements = cast(list[str], _project()['optional-dependencies'][extra])
    httpx = [Requirement(item) for item in requirements if Requirement(item).name == 'httpx']

    assert httpx, f'期望: extra {extra} 声明 httpx。实际: 没有。下一步: 加上 "httpx>=0.27,<1"。'
    specifier = httpx[0].specifier
    assert not specifier.contains('1.0.0.dev6', prereleases=True), (
        f'期望: extra {extra} 的 httpx 封顶在 1 以下（pip install --pre 会装进 1.0 的预发布'
        f'重写版）。实际: {httpx[0]}。下一步: 写成 "httpx>=0.27,<1"。'
    )
    assert specifier.contains('0.28.1'), f'期望: 锁定的 httpx 0.28.1 仍满足 {httpx[0]}。'


def test_project_urls_point_at_the_repository() -> None:
    urls = cast(dict[str, str], _project()['urls'])

    assert list(urls) == list(URLS)
    assert all(url.startswith('https://github.com/ai-loren/syndica') for url in urls.values())


@pytest.mark.parametrize('classifier', REQUIRED_CLASSIFIERS)
def test_required_classifier_is_declared(classifier: str) -> None:
    assert classifier in _project()['classifiers']


def test_no_classifier_claims_windows_or_a_legacy_license() -> None:
    forbidden = [
        name
        for name in _project()['classifiers']
        if name.startswith(('Operating System :: Microsoft', 'License ::'))
        or name == 'Operating System :: OS Independent'
    ]

    assert forbidden == []


def test_version_is_a_canonical_public_pep_440_version() -> None:
    version = _project()['version']
    parsed = Version(version)

    assert str(parsed) == version
    assert parsed.local is None
    assert parsed.epoch == 0


def test_syndica_version_prints_the_pyproject_version(tmp_path: Path) -> None:
    script = Path(sys.executable).with_name('syndica')
    assert script.is_file(), f'{script} 不存在：先 uv sync 装好 syndica 的命令行入口'

    completed = subprocess.run(
        [str(script), '--version'], capture_output=True, text=True, cwd=tmp_path, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f'syndica {_project()["version"]}\n', (
        '装着的元数据与 pyproject.toml 不一致：改过 version 之后 uv sync 一次'
    )


def test_changelog_has_an_entry_and_a_withdrawn_names_section_for_this_version() -> None:
    text = (REPO_ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    version = _project()['version']

    assert f'\n## [{version}] - ' in text
    assert '\n### Breaking changes before 1.0\n' in text
    assert f'\n[{version}]: https://github.com/ai-loren/syndica/' in text


def test_blank_issues_are_off_and_security_reports_go_to_private_advisories() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / '.github' / 'ISSUE_TEMPLATE' / 'config.yml').read_text(encoding='utf-8')
    )

    assert config['blank_issues_enabled'] is False
    assert 'https://github.com/ai-loren/syndica/security/advisories/new' in [
        link['url'] for link in config['contact_links']
    ]


@pytest.mark.parametrize('form', ['bug_report.yml', 'feature_request.yml'])
def test_issue_form_has_unique_field_ids(form: str) -> None:
    document = yaml.safe_load(
        (REPO_ROOT / '.github' / 'ISSUE_TEMPLATE' / form).read_text(encoding='utf-8')
    )
    ids = [field['id'] for field in document['body'] if 'id' in field]

    assert document['name'] and document['description']
    assert len(ids) == len(set(ids)) >= 3


def test_check_dist_on_an_empty_dist_names_uv_build_and_drops_stale_checksums(
    tmp_path: Path,
) -> None:
    stale = tmp_path / 'SHA256SUMS'
    stale.write_text('0' * 64 + '  syndica-0.0.0-py3-none-any.whl\n', encoding='utf-8')
    (tmp_path / 'syndica-0.0.0-py3-none-any.whl').write_bytes(b'')

    completed = subprocess.run(
        [sys.executable, str(CHECK_DIST), '--root', str(REPO_ROOT), '--dist', str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert 'uv build' in completed.stderr
    assert 'syndica-0.0.0-py3-none-any.whl' in completed.stderr
    assert not stale.exists()
