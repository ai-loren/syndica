"""v1.0 公开 API 快照：快照的 diff 就是公开面的变化，由人 review。

`SYNDICA_UPDATE_PUBLIC_API=1` 重写快照与英文参考页；不设时逐字比对。加名字不破坏，删名字、改签名
破坏——测试变红时先看 diff，再决定改快照还是改代码。
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _generator() -> ModuleType:
    module = sys.modules.get('syndica_public_api')
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        'syndica_public_api', _ROOT / 'scripts' / 'public_api.py'
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['syndica_public_api'] = module
    spec.loader.exec_module(module)
    return module


def _update() -> bool:
    return os.environ.get('SYNDICA_UPDATE_PUBLIC_API') == '1'


@pytest.fixture(scope='module')
def rendered() -> tuple[str, str]:
    generator = _generator()
    snapshot = generator.render_snapshot(generator.snapshot())
    page = generator.render_reference(json.loads(snapshot), generator.descriptions())
    return snapshot, page


def test_the_public_api_snapshot_matches_the_code(rendered: tuple[str, str]):
    generator = _generator()
    snapshot, _ = rendered
    if _update():
        generator.SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        generator.SNAPSHOT.write_text(snapshot, encoding='utf-8')
    assert generator.SNAPSHOT.read_text(encoding='utf-8') == snapshot, (
        '公开面变了：SYNDICA_UPDATE_PUBLIC_API=1 重新生成 tests/testdata/public_api.json，看 diff；'
        '删掉的名字与改掉的签名是破坏性变更，要写进 CHANGELOG'
    )


def test_the_api_reference_page_matches_the_snapshot(rendered: tuple[str, str]):
    generator = _generator()
    _, page = rendered
    if _update():
        generator.REFERENCE.parent.mkdir(parents=True, exist_ok=True)
        generator.REFERENCE.write_text(page, encoding='utf-8')
    assert generator.REFERENCE.read_text(encoding='utf-8') == page, (
        'docs/user/reference/api.md 与快照不一致：SYNDICA_UPDATE_PUBLIC_API=1 重新生成，别手改'
    )


def test_every_public_name_has_a_description():
    generator = _generator()
    assert generator.missing_descriptions(generator.public_names(), generator.descriptions()) == []


def test_every_description_belongs_to_a_public_name():
    generator = _generator()
    assert generator.stray_descriptions(generator.public_names(), generator.descriptions()) == []


def test_descriptions_are_single_english_lines():
    generator = _generator()
    lines = [
        (f'{module}.{name}', text)
        for module, described in generator.descriptions().items()
        for name, text in described.items()
    ]
    assert [name for name, text in lines if '\n' in text or not text.strip()] == []
    assert [name for name, text in lines if re.search(r'[\u4e00-\u9fff]', text)] == []


# 说明里用反引号点名的外部类：不是 syndica 的名字，也不该被当成「点名了一个私有名字」。
_EXTERNAL_NAMES = frozenset({'DPOTrainer', 'None', 'SFTTrainer'})


def test_descriptions_only_name_public_classes_and_constants():
    """说明是给只读这一页的人看的：点名一个私有的类，等于让人去 import 一个不承诺的名字。"""
    generator = _generator()
    public = {name for names in generator.public_names().values() for name in names}
    named = {
        (f'{module}.{name}', token.split('.')[0])
        for module, described in generator.descriptions().items()
        for name, text in described.items()
        for token in re.findall(r'`([A-Z][A-Za-z0-9_]*(?:\.[a-z_]+)?)`', text)
    }
    assert (
        sorted((where, token) for where, token in named if token not in public | _EXTERNAL_NAMES)
        == []
    )


def test_the_snapshot_is_machine_independent(rendered: tuple[str, str]):
    snapshot, page = rendered
    for text in (snapshot, page):
        assert re.findall(r'0x[0-9a-fA-F]{6,}', text) == []
        assert re.findall(r'(?:/Users/|/home/|/private/|[A-Z]:\\)', text) == []
        assert str(_ROOT) not in text


def test_rendering_twice_gives_the_same_bytes(rendered: tuple[str, str]):
    generator = _generator()
    assert generator.render_snapshot(generator.snapshot()) == rendered[0]


def test_the_cli_and_the_server_are_surfaces_not_python_api():
    generator = _generator()
    modules = set(generator.public_names())
    assert sorted(generator.SURFACES) == ['syndica.cli', 'syndica.server']
    assert [name for name in modules if name.startswith(('syndica.cli', 'syndica.server'))] == []


def test_the_kernel_subpackages_are_not_separate_import_paths():
    """`syndica.kernel` 是 kernel 一侧唯一的导入路径：子包的 `__all__` 里有刻意扣留的名字。"""
    generator = _generator()
    subpackages = ('agents', 'hooks', 'llm', 'protocols', 'stores', 'tools')
    assert [
        name
        for name in generator.public_names()
        if name.removeprefix('syndica.kernel.') in subpackages
    ] == []


def test_the_names_promised_through_submodules_agree_with_the_kernel_guard():
    """两份清单各自逐字写死：快照按这一份生成，`tests/kernel/test_public_api.py` 按那一份守
    `__all__`。"""
    spec = importlib.util.spec_from_file_location(
        'syndica_kernel_surface_guard', _ROOT / 'tests' / 'kernel' / 'test_public_api.py'
    )
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    expected = {module: sorted(names) for module, names in guard._PROMISED_VIA_SUBMODULE.items()}
    actual = {
        module: sorted(names) for module, names in _generator().PROMISED_VIA_SUBMODULE.items()
    }
    assert actual == expected


def test_the_snapshot_records_the_http_digest_that_the_change_log_lists():
    generator = _generator()
    http = generator.snapshot()['http']
    changes = (_ROOT / 'docs' / 'reference' / 'api-changes.md').read_text(encoding='utf-8')
    assert f'`{http["sha256_12"]}`' in changes


# 公开签名里出现、却刻意不公开的类型：每一项都写明为什么调用方不需要按名字拿到它。新出现的一个会
# 让下面那条用例变红——要么公开它，要么在这里回答一次。
_OPAQUE: dict[str, str] = {
    # 不透明的句柄：调用方只把 `prepare` / `stage_isolation` 交回来的东西原样传给下一步。
    'syndica.kernel.isolation.CopyTicket': 'opaque handle',
    'syndica.kernel.isolation._Prepared': 'opaque handle',
    'syndica.kernel.workspace_router._Staged': 'opaque handle',
    # `Subscription` 只由 `EventBus.subscribe` 构造。
    'syndica.kernel.events._Buffer': 'constructed by EventBus',
    # 结构化的 Runtime 侧面：传 `Runtime` 即满足。
    'syndica.kernel.danger._ConfirmationRuntime': 'structural facet of Runtime',
    'syndica.kernel.tools.human._HumanRuntime': 'structural facet of Runtime',
    # `ANY` 的类型；调用方写 `ANY`，标注写公开的 `Expected`。
    'syndica.kernel.workspace._Any': 'type of the public ANY sentinel',
    # `Runtime(artifacts=)` 是 v1.0 provisional 的参数，参考页写明。
    'syndica.kernel.protocols.artifact.ArtifactStore': 'provisional',
}


def _syndica_classes(hint: object) -> Iterator[type]:
    if isinstance(hint, type) and hint.__module__.startswith('syndica.'):
        yield hint
    for argument in typing.get_args(hint):
        yield from _syndica_classes(argument)


def _callables(value: object) -> Iterator[tuple[str, Callable[..., object]]]:
    if inspect.isfunction(value):
        yield '', value
        return
    if not isinstance(value, type):
        return
    if dataclasses.is_dataclass(value):
        yield '', value
    for name in dir(value):
        if name.startswith('_') and name != '__init__':
            continue
        owner = next((klass for klass in value.__mro__ if name in vars(klass)), None)
        if owner is None or not owner.__module__.startswith('syndica'):
            continue
        raw = vars(owner)[name]
        if isinstance(raw, staticmethod | classmethod):
            raw = raw.__func__
        if isinstance(raw, property):
            raw = raw.fget
        if inspect.isfunction(raw):
            yield f'.{name}', raw


def test_public_signatures_name_only_public_or_deliberately_opaque_types():
    generator = _generator()
    names = generator.public_names()
    objects = {
        f'{module}.{name}': getattr(importlib.import_module(module), name)
        for module, module_names in names.items()
        for name in module_names
    }
    public = {id(value) for value in objects.values()}
    # 只为类型检查导入的名字（`if TYPE_CHECKING:`）不在模块的命名空间里：从公开面补上。
    fallback: dict[str, Any] = {path.rsplit('.', 1)[1]: value for path, value in objects.items()}
    leaked: dict[str, set[str]] = {}
    for path, value in objects.items():
        for suffix, function in _callables(value):
            hints = typing.get_type_hints(function, localns=fallback)
            for hint in hints.values():
                for found in _syndica_classes(hint):
                    if id(found) not in public:
                        leaked.setdefault(f'{found.__module__}.{found.__qualname__}', set()).add(
                            path + suffix
                        )
    assert sorted(set(leaked) - set(_OPAQUE)) == [], {
        name: sorted(users) for name, users in leaked.items() if name not in _OPAQUE
    }
    assert sorted(set(_OPAQUE) - set(leaked)) == []
