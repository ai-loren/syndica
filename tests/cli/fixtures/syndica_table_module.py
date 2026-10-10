"""A third-party Module that contributes an artifact handler, loaded through a `syndica.modules`
entry point (`table-check = syndica_table_module:build`).

`TableHandler` takes over CSV deliverables (`spreadsheet` / `text/csv`, the pair L1 routes `.csv`
files to) and adds a `table` kind that `Deliver` accepts once the handler is installed. It flags a
CSV whose rows do not all have the same number of columns.
"""

from __future__ import annotations

from syndica.kernel import (
    ArtifactReader,
    ArtifactRef,
    Diff,
    ModuleHost,
    Preview,
    RuntimeModule,
    Violation,
)

__all__ = ['TableHandler', 'build']


class TableHandler:
    handles: tuple[tuple[str, str], ...] = (('spreadsheet', 'text/csv'), ('table', 'text/csv'))

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:
        rows = (await reader.read(ref)).splitlines()
        return Preview(text=f'{len(rows)} rows')

    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Diff:
        before, after = (await reader.read(a)).splitlines(), (await reader.read(b)).splitlines()
        return Diff(summary=f'{len(before)} -> {len(after)} rows', changed=before != after)

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]:
        rows = [line.split(',') for line in (await reader.read(ref)).splitlines() if line]
        width = len(rows[0]) if rows else 0
        return [
            Violation(
                rule='table.ragged',
                message=f'row {number} has {len(row)} columns, expected {width}',
                anchor=f'{ref.path}#L{number}',
            )
            for number, row in enumerate(rows, start=1)
            if len(row) != width
        ]


def build(host: ModuleHost) -> RuntimeModule:
    del host
    return RuntimeModule(
        'table-check',
        version='0.1.0',
        description='Checks that every row of a delivered CSV has the same number of columns',
        capabilities=(),
        artifact_handlers=(TableHandler(),),
    )
