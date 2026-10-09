"""`syndica export` 读工作区里的四个 store，写出训练集。不改那些 store。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from syndica.kernel.stores.file_artifact import FileArtifactStore
from syndica.kernel.stores.file_rubric import FileRubricStore
from syndica.kernel.stores.file_session import FileSessionStore
from syndica.kernel.stores.file_trace import FileTraceStore
from syndica.runner.assemble import assemble_attempts
from syndica.runner.training import write_export

__all__ = ['export_workspace']


def export_workspace(
    sessions: Path,
    traces: Path,
    rubrics: Path,
    artifacts: Path,
    fmt: str,
    output: Path,
    *,
    include_unverified: bool = False,
) -> tuple[str, int]:
    """导出。有会话没拼成时退出码是 1，已经拼成的仍然写出。"""
    attempts, failed = asyncio.run(
        assemble_attempts(
            FileSessionStore(sessions),
            FileTraceStore(traces),
            FileRubricStore(rubrics),
            FileArtifactStore(artifacts),
        )
    )
    tokenize = _utf8 if fmt == 'rl' else None
    manifest = write_export(
        attempts,
        fmt,
        output,
        include_unverified=include_unverified,
        tokenize=tokenize,
        failed=failed,
    )
    sources = manifest.get('sources')
    count = len(cast(list[object], sources)) if isinstance(sources, list) else 0
    text = f'wrote {count} to {output}\n'
    return text, 1 if failed else 0


def _utf8(text: str) -> list[int]:
    """没有模型分词器时的占位：每个 UTF-8 字节一个 id。TRL 对接换它自己的分词器。"""
    return list(text.encode('utf-8'))
