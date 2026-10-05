"""两份宿主私有目录名单必须一字不差地相等（M7 评审 B5）。

文件工具的禁写名单在 `cli.host._PRIVATE`（全路径），shell 侧判定在 `danger._HOST_RECORDS`（名字）。
两份各写一处，任何一边漏一个目录，Agent 就能从没堵上的那条路径伪造宿主记录——改会话 / 记忆的 trust，
或把 proposed Rubric 改成 active 给自己出题。这里把「两份是同一份名单」从注释升级成断言。
"""

from __future__ import annotations

from syndica.cli.host import _PRIVATE
from syndica.kernel.danger import _HOST_RECORDS


def test_the_two_host_record_lists_name_exactly_the_same_directories() -> None:
    from_private = {path.removeprefix('.syndica/') for path in _PRIVATE}
    assert from_private == set(_HOST_RECORDS)
    # 两边都没有重复项（去重后计数相等），否则一份悄悄多一个名字也会被上面的集合比较吃掉。
    assert len(_PRIVATE) == len(from_private)
    assert len(_HOST_RECORDS) == len(set(_HOST_RECORDS))


def test_the_acceptance_store_directories_are_private() -> None:
    # 产物与 Rubric 库存着验收状态与 trust：M7 的两个新目录必须在两份名单里（评审 B5）。
    for name in ('artifacts', 'rubrics'):
        assert f'.syndica/{name}' in _PRIVATE
        assert name in _HOST_RECORDS
