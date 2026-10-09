"""`syndica export` 读工作区里的四个 store，写出训练集。不改那些 store。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from syndica.kernel import ConfigError, Redactor
from syndica.kernel.stores.file_artifact import FileArtifactStore
from syndica.kernel.stores.file_rubric import FileRubricStore
from syndica.kernel.stores.file_session import FileSessionStore
from syndica.kernel.stores.file_trace import FileTraceStore
from syndica.runner.assemble import assemble_attempts
from syndica.runner.training import write_export

__all__ = ['export_workspace']


def export_workspace(
    workspace: Path,
    stores: tuple[Path, Path, Path, Path],
    fmt: str,
    output: Path,
    *,
    redactor: Redactor,
    secrets: Sequence[str] = (),
    include_unverified: bool = False,
) -> tuple[str, int]:
    """导出。`stores` 是会话、trace、Rubric、产物四个目录。

    `output` 不能在工作区里：导出的文件里有别的会话的全文与不可信内容，放在工作区里，下一次 run 会
    把它当成可信的基线读进来。有会话或 trace 读不出来时退出码是 1，读出来的照样写出。
    """
    resolved = output.expanduser().resolve()
    root = workspace.resolve()
    if resolved == root or root in resolved.parents:
        raise ConfigError(
            expected='--output 在工作区之外',
            received=f'{resolved} 在工作区 {root} 之内',
            next_action=(
                '换一个工作区之外的目录，例如 ~/datasets/<名字>；放在工作区里，'
                'Agent 读得到别的会话的全文，下一次 run 还会把它当成可信内容'
            ),
        )
    sessions, traces, rubrics, artifacts = stores
    assembled = asyncio.run(
        assemble_attempts(
            FileSessionStore(sessions),
            FileTraceStore(traces),
            FileRubricStore(rubrics),
            FileArtifactStore(artifacts),
        )
    )
    tokenize = _utf8 if fmt == 'rl' else None
    manifest = write_export(
        assembled.attempts,
        fmt,
        resolved,
        include_unverified=include_unverified,
        secrets=secrets,
        tokenize=tokenize,
        tokenizer='utf8-bytes' if fmt == 'rl' else None,
        failed=assembled.failed,
        skipped_traces=assembled.skipped_traces,
        redactor=redactor,
    )
    sources = manifest.get('sources')
    count = len(cast(list[object], sources)) if isinstance(sources, list) else 0
    failed = cast(list[object], manifest.get('failed') or [])
    skipped = cast(list[object], manifest.get('skipped_traces') or [])
    text = f'写出 {count} 条到 {resolved}\n'
    if failed or skipped:
        text += (
            f'期望: 每个会话与 trace 都读得出来\n'
            f'实际: {len(failed)} 条会话没拼成，{len(skipped)} 个 trace 读不回来\n'
            f'下一步: 看 {resolved / "manifest.json"} 的 failed 与 skipped_traces；'
            '其余样本已经写出\n'
        )
        return text, 1
    return text, 0


def _utf8(text: str) -> list[int]:
    """没有模型分词器时的占位：每个 UTF-8 字节一个 id。TRL 对接换它自己的分词器。"""
    return list(text.encode('utf-8'))
