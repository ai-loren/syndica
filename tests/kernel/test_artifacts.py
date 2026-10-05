"""产物处理器注册表（§7.2）：更具体优先、同精度冲突报错、generic 兜底。"""

from __future__ import annotations

import pytest

from syndica.kernel.artifacts import (
    ArtifactHandlers,
    ArtifactReader,
    Diff,
    GenericHandler,
    Preview,
    Violation,
)
from syndica.kernel.context import ArtifactRef
from syndica.kernel.errors import ConfigError


def _ref(path: str, *, kind: str, mime: str, version: str = 'v1') -> ArtifactRef:
    return ArtifactRef(
        id=f'file:{path}', path=path, kind=kind, mime=mime, version=version, producer='agent-1'
    )


class _Reader:
    def __init__(self, content: dict[tuple[str, str], str]) -> None:
        self._content = content

    async def read(self, ref: ArtifactRef) -> str:
        try:
            return self._content[(ref.path, ref.version)]
        except KeyError:
            raise FileNotFoundError(ref.path) from None


class _SheetHandler:
    handles: tuple[tuple[str, str], ...] = (('spreadsheet', 'text/csv'),)

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:
        return Preview(text='sheet')

    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Diff:
        return Diff(summary='sheet diff')

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]:
        return []


class _KindWideHandler:
    handles: tuple[tuple[str, str], ...] = (('spreadsheet', '*'),)

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:
        return Preview(text='kind-wide')

    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Diff:
        return Diff(summary='kind-wide diff')

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]:
        return []


def test_an_exact_kind_mime_wins_over_a_kind_wildcard() -> None:
    registry = ArtifactHandlers()
    registry.register(_KindWideHandler())
    registry.register(_SheetHandler())
    exact = registry.for_(_ref('a.csv', kind='spreadsheet', mime='text/csv'))
    assert isinstance(exact, _SheetHandler)
    # 同 kind、别的 mime → 落到 kind 通配，不落 generic。
    wide = registry.for_(_ref('a.tsv', kind='spreadsheet', mime='text/tab-separated-values'))
    assert isinstance(wide, _KindWideHandler)


def test_an_unmatched_artifact_falls_back_to_generic() -> None:
    registry = ArtifactHandlers()
    handler = registry.for_(_ref('a.md', kind='document', mime='text/markdown'))
    assert isinstance(handler, GenericHandler)


def test_registering_the_same_precision_twice_is_a_startup_error() -> None:
    registry = ArtifactHandlers()
    registry.register(_SheetHandler())
    with pytest.raises(ConfigError) as caught:
        registry.register(_SheetHandler())
    # 三件套点名冲突的 (kind, mime) 与两个处理器，便于调用方自己解。
    assert 'spreadsheet' in caught.value.expected
    assert '_SheetHandler' in caught.value.received


def test_a_second_global_default_conflicts_with_the_prebuilt_generic() -> None:
    # generic 预注册为 ('*','*')：第二个全局兜底是两个全局默认冲突，不是静默覆盖。
    registry = ArtifactHandlers()
    with pytest.raises(ConfigError):
        registry.register(GenericHandler())


async def test_generic_validate_flags_an_empty_artifact() -> None:
    reader = _Reader({('a.md', 'v1'): '   \n'})
    violations = await GenericHandler().validate(
        _ref('a.md', kind='document', mime='text/markdown'), reader
    )
    assert [v.rule for v in violations] == ['empty']


async def test_generic_validate_passes_a_nonempty_artifact() -> None:
    reader = _Reader({('a.md', 'v1'): '# Title\n'})
    assert (
        await GenericHandler().validate(_ref('a.md', kind='document', mime='text/markdown'), reader)
        == []
    )


async def test_generic_semantic_diff_reports_no_change_for_equal_versions() -> None:
    reader = _Reader({('a.md', 'v1'): 'same\n', ('a.md', 'v2'): 'same\n'})
    a = _ref('a.md', kind='document', mime='text/markdown', version='v1')
    b = _ref('a.md', kind='document', mime='text/markdown', version='v2')
    diff = await GenericHandler().semantic_diff(a, b, reader)
    assert diff.changed is False


async def test_generic_semantic_diff_counts_changed_lines() -> None:
    reader = _Reader({('a.md', 'v1'): 'one\ntwo\n', ('a.md', 'v2'): 'one\nTWO\n'})
    a = _ref('a.md', kind='document', mime='text/markdown', version='v1')
    b = _ref('a.md', kind='document', mime='text/markdown', version='v2')
    diff = await GenericHandler().semantic_diff(a, b, reader)
    assert diff.changed is True
