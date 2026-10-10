"""`RuntimeModule(artifact_handlers=...)` 的形状检查，与注册表按 Module 一批装、要么全装要么
一个不留。"""

from __future__ import annotations

from typing import Any

import pytest

from syndica.kernel import (
    ArtifactHandler,
    ArtifactReader,
    ArtifactRef,
    ConfigError,
    Diff,
    Preview,
    RuntimeModule,
    Violation,
)
from syndica.kernel.artifacts import ArtifactHandlers, GenericHandler


class _Handler:
    def __init__(self, *handles: tuple[str, str]) -> None:
        self.handles: tuple[tuple[str, str], ...] = handles or (('spreadsheet', 'text/csv'),)

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:
        return Preview(text='')

    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Diff:
        return Diff(summary='')

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]:
        return []


class _SyncPreview(_Handler):
    def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:  # type: ignore[override]
        return Preview(text='')


def _ref(kind: str, mime: str) -> ArtifactRef:
    return ArtifactRef(id='file:a', path='a', kind=kind, mime=mime, version='v', producer='p')


def test_a_module_keeps_the_handlers_it_declares_in_order():
    first, second = _Handler(('spreadsheet', 'text/csv')), _Handler(('table', '*'))
    module = RuntimeModule('tables', artifact_handlers=[first, second])
    assert module.artifact_handlers == (first, second)
    assert RuntimeModule('plain').artifact_handlers == ()


def test_the_handler_protocol_is_satisfied_by_a_plain_class():
    assert isinstance(_Handler(), ArtifactHandler)


@pytest.mark.parametrize(
    ('handlers', 'anchor'),
    [
        ((_Handler,), '加上括号'),
        ((object(),), 'ArtifactHandler'),
        ((_SyncPreview(),), 'preview'),
        ((_Handler(('Table', 'text/csv')),), 'handles'),
        ((_Handler(('table', 'text/csv\n')),), 'handles'),
        ((_Handler(('table', 'csv')),), 'handles'),
        ('handler', 'list 或 tuple'),
    ],
    ids=[
        'class-not-instance',
        'not-a-handler',
        'sync-method',
        'uppercase-kind',
        'newline-in-mime',
        'mime-without-subtype',
        'string-not-sequence',
    ],
)
def test_a_malformed_handler_declaration_is_refused_with_the_field_to_fix(
    handlers: Any, anchor: str
):
    with pytest.raises(ConfigError) as caught:
        RuntimeModule('tables', artifact_handlers=handlers)
    assert anchor in caught.value.message


def test_a_handler_with_no_pairs_is_refused():
    empty = _Handler()
    empty.handles = ()
    with pytest.raises(ConfigError) as caught:
        RuntimeModule('tables', artifact_handlers=(empty,))
    assert 'handles' in caught.value.expected


def test_a_batch_with_one_conflicting_handler_installs_none_of_them():
    registry = ArtifactHandlers()
    registry.register(_Handler(('spreadsheet', 'text/csv')))
    fresh, clashing = _Handler(('table', 'text/csv')), _Handler(('spreadsheet', 'text/csv'))

    with pytest.raises(ConfigError) as caught:
        registry.register_all((fresh, clashing), source="Module 'tables'")

    assert "Module 'tables'" in caught.value.received
    assert 'table' not in registry.kinds()
    assert isinstance(registry.for_(_ref('table', 'text/csv')), GenericHandler)


def test_two_handlers_of_one_batch_claiming_the_same_pair_conflict():
    registry = ArtifactHandlers()
    with pytest.raises(ConfigError):
        registry.register_all((_Handler(('table', '*')), _Handler(('table', '*'))))
    assert 'table' not in registry.kinds()


def test_one_handler_listing_a_pair_twice_is_not_a_conflict_with_itself():
    registry = ArtifactHandlers()
    handler = _Handler(('table', '*'), ('table', '*'))
    registry.register_all((handler,))
    assert registry.for_(_ref('table', 'text/plain')) is handler
