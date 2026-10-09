"""`syndica export` 读工作区里的四个 store，写出训练集。不改那些 store。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from syndica.kernel import ConfigError, Redactor
from syndica.kernel.stores.file_artifact import FileArtifactStore
from syndica.kernel.stores.file_rubric import FileRubricStore
from syndica.kernel.stores.file_session import FileSessionStore
from syndica.kernel.stores.file_trace import FileTraceStore
from syndica.runner.assemble import Assembled, assemble_attempts
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
    把它当成可信的基线读进来。`output` 本身也不能是符号链接；上级是链接（macOS 的 `/tmp`）照常。
    有会话或 trace 读不出来时退出码是 1，读出来的照样写出。
    """
    given = output.expanduser().absolute()
    # 必须在 resolve 之前查：resolve 之后看到的是链接指向的地方，写盘那一侧的检查就认不出链接了。
    if given.is_symlink():
        raise ConfigError(
            expected='--output 是一个真目录或还不存在的路径',
            received=f'{given} 是符号链接',
            next_action='把 --output 换成一个不是符号链接的目录；导出不跟着链接写到它指向的地方',
        )
    resolved = given.resolve()
    root = workspace.resolve()
    if _within(resolved, root):
        raise ConfigError(
            expected='--output 在工作区之外',
            received=f'{resolved} 在工作区 {root} 之内',
            next_action=(
                '换一个工作区之外的目录，例如 ~/datasets/<名字>；放在工作区里，'
                'Agent 读得到别的会话的全文，下一次 run 还会把它当成可信内容'
            ),
        )
    assembled, unassembled, unread = asyncio.run(_read(stores))
    tokenize = _utf8 if fmt == 'rl' else None
    manifest = write_export(
        assembled.attempts,
        fmt,
        resolved,
        include_unverified=include_unverified,
        secrets=secrets,
        tokenize=tokenize,
        tokenizer='utf8-bytes' if fmt == 'rl' else None,
        failed=unassembled,
        skipped_traces=unread,
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


async def _read(stores: tuple[Path, Path, Path, Path]) -> tuple[Assembled, list[str], list[str]]:
    """拼好的尝试，加上会话与 trace 的失败名单。`list_metas()` 与 `traces()` 不交出读不出来的那些，
    拼的时候也就看不见它们：不从两个 store 另外取回来，它们就从导出里无声消失，退出码还是 0。"""
    sessions, traces, rubrics, artifacts = stores
    session_store = FileSessionStore(sessions)
    trace_store = FileTraceStore(traces)
    assembled = await assemble_attempts(
        session_store, trace_store, FileRubricStore(rubrics), FileArtifactStore(artifacts)
    )
    failed = [
        *assembled.failed,
        *(
            f'{sid}: {received}；下一步: {next_action}'
            for sid, received, next_action in await session_store.unreadable()
        ),
    ]
    skipped = [
        *assembled.skipped_traces,
        *(
            f'{traces / name}: {received}；下一步: {next_action}'
            for name, received, next_action in await trace_store.unreadable()
        ),
    ]
    return assembled, failed, skipped


def _within(path: Path, root: Path) -> bool:
    """`path` 或它的某一级上级就是 `root` 这个目录。按设备号与 inode 认，不按路径字符串：
    不区分大小写的文件系统（macOS 默认）上换了大小写的路径是同一个目录，字符串比较认不出来。
    还不存在的那几级跳过。"""
    for candidate in (path, *path.parents):
        try:
            if os.path.samefile(candidate, root):
                return True
        except OSError:
            continue
    return False


def _utf8(text: str) -> list[int]:
    """没有模型分词器时的占位：每个 UTF-8 字节一个 id。TRL 对接换它自己的分词器。"""
    return list(text.encode('utf-8'))
