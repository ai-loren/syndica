"""Deliver 工具（§7.4）：把已写出的文件标成交付物、回报带 is_deliverable 的产物；缺文件给错。"""

from __future__ import annotations

from pathlib import Path

from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tools.local import Deliver, Write


def _portal(root: Path) -> object:
    return LocalWorkspace(str(root), shell='/bin/sh', env={}).portal()


async def test_deliver_marks_a_written_file_as_a_deliverable(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    portal = _portal(root)
    version = await portal.write_text('report.md', '# Report\n')  # pyright: ignore[reportAttributeAccessIssue]
    result = await Deliver(portal).execute('report.md', note='final report')  # pyright: ignore[reportArgumentType]
    assert len(result.artifacts) == 1
    ref = result.artifacts[0]
    assert ref.id == 'file:report.md'
    assert ref.is_deliverable is True
    assert ref.version == version  # 当前版本，不新写一版
    assert ref.kind == 'document'  # 按扩展名
    assert result.meta.get('note') == 'final report'


async def test_deliver_can_override_the_kind(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    portal = _portal(root)
    await portal.write_text('data.bin', 'x')  # pyright: ignore[reportAttributeAccessIssue]
    result = await Deliver(portal).execute('data.bin', kind='dataset')  # pyright: ignore[reportArgumentType]
    assert result.artifacts[0].kind == 'dataset'  # 显式 kind 覆盖扩展名推断（§7.4）


async def test_deliver_on_a_missing_path_returns_an_actionable_error(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    result = await Deliver(_portal(root)).execute('nope.md')  # pyright: ignore[reportArgumentType]
    assert result.artifacts == []
    assert 'nope.md' in result.output
    assert '先写出' in result.output  # 下一步可执行


async def test_a_delivered_file_is_not_a_second_write(tmp_path: Path) -> None:
    # Deliver 只标记、不改内容：文件内容不变。
    root = tmp_path / 'ws'
    root.mkdir()
    portal = _portal(root)
    await Write(portal).execute('report.md', 'body\n')  # pyright: ignore[reportArgumentType]
    await Deliver(portal).execute('report.md')  # pyright: ignore[reportArgumentType]
    assert (root / 'report.md').read_text(encoding='utf-8') == 'body\n'
