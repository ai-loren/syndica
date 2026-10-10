"""错误分类是 v1.0 公开 API 的一部分：每个公开异常的直接父类逐字写死，分界写在
`docs/user/reference/errors.md`。加子类不破坏；把一个类挪到别的父类下面，接住旧父类的调用方就接不住它了。"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
from pathlib import Path

import pytest

import syndica.kernel as kernel
from syndica.runner import ScenarioMismatch

_ROOT = Path(__file__).resolve().parents[1]
_PAGE = _ROOT / 'docs' / 'user' / 'reference' / 'errors.md'

# 公开异常 → 它的直接父类（公开路径，或标准库的类名）。
_HIERARCHY: dict[str, str] = {
    'syndica.kernel.SyndicaError': 'Exception',
    'syndica.kernel.ConfigError': 'syndica.kernel.SyndicaError',
    'syndica.kernel.ContractViolation': 'syndica.kernel.SyndicaError',
    'syndica.kernel.ToolFatalError': 'syndica.kernel.SyndicaError',
    'syndica.kernel.LLMResponseError': 'syndica.kernel.SyndicaError',
    'syndica.kernel.ContextOverflowError': 'syndica.kernel.SyndicaError',
    'syndica.kernel.PayloadRefused': 'syndica.kernel.SyndicaError',
    'syndica.kernel.WriteConflict': 'Exception',
    'syndica.backends.llm_openai.ProviderHTTPError': 'syndica.kernel.LLMResponseError',
    'syndica.backends.otel_otlp.OtlpExportError': 'syndica.kernel.SyndicaError',
    'syndica.runner.ScenarioMismatch': 'syndica.kernel.ContractViolation',
}


def _resolve(path: str) -> type:
    if '.' not in path:
        return getattr(builtins, path)
    module, name = path.rsplit('.', 1)
    return getattr(importlib.import_module(module), name)


def _public_exceptions() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        'syndica_public_api_for_errors', _ROOT / 'scripts' / 'public_api.py'
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    found: set[str] = set()
    for module, names in generator.public_names().items():
        for name in names:
            value = getattr(importlib.import_module(module), name)
            if isinstance(value, type) and issubclass(value, BaseException):
                found.add(f'{module}.{name}')
    return found


def test_the_hierarchy_covers_every_public_exception():
    assert sorted(_public_exceptions()) == sorted(_HIERARCHY)


@pytest.mark.parametrize(('name', 'parent'), sorted(_HIERARCHY.items()))
def test_each_public_exception_keeps_its_direct_parent(name: str, parent: str):
    assert _resolve(name).__bases__ == (_resolve(parent),)


def test_write_conflict_is_deliberately_not_a_syndica_error():
    assert not issubclass(kernel.WriteConflict, kernel.SyndicaError)


@pytest.mark.parametrize(
    'error',
    [kernel.ConfigError, kernel.ContractViolation, kernel.ToolFatalError, ScenarioMismatch],
)
def test_the_three_parts_are_keyword_only_and_survive_on_the_instance(
    error: type[kernel.SyndicaError],
):
    raised = error(expected='e', received='r', next_action='n')
    assert (raised.expected, raised.received, raised.next_action) == ('e', 'r', 'n')
    with pytest.raises(TypeError):
        error('e', 'r', 'n')  # pyright: ignore[reportCallIssue]


def test_the_page_documents_every_public_exception():
    page = _PAGE.read_text(encoding='utf-8')
    names = sorted({path.rsplit('.', 1)[1] for path in _HIERARCHY})
    assert [name for name in names if f'`{name}`' not in page] == []
