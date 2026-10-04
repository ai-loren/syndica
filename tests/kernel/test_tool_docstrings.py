"""内置工具的参数描述都在一行里：反射只认单行 `name: 描述`，续行静默丢掉，模型看到的是半句话。"""

from __future__ import annotations

import inspect

import pytest

import syndica.kernel.tools.human  # noqa: F401  # pyright: ignore[reportUnusedImport]
import syndica.kernel.tools.local  # noqa: F401  # pyright: ignore[reportUnusedImport]
import syndica.kernel.tools.orchestration  # noqa: F401  # pyright: ignore[reportUnusedImport]
from syndica.kernel.tool import Tool


def _builtin_tools() -> list[type[Tool]]:
    found: list[type[Tool]] = []
    pending = list(Tool.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith('syndica.kernel.tools.') and 'execute' in vars(cls):
            found.append(cls)
    return sorted(found, key=lambda cls: cls.__qualname__)


def test_the_guard_sees_the_tools_whose_descriptions_wrap_most_often():
    names = {cls.__qualname__ for cls in _builtin_tools()}

    assert {'Terminal', 'Delegate', 'Fork', 'Bash'} <= names


@pytest.mark.parametrize('cls', _builtin_tools(), ids=lambda cls: cls.__qualname__)
def test_every_parameter_description_fits_on_its_own_line(cls: type[Tool]):
    execute = vars(cls)['execute']
    names = set(inspect.signature(execute).parameters) - {'self'}
    lines = inspect.cleandoc(execute.__doc__ or '').splitlines()
    first = next(
        (index for index, line in enumerate(lines) if line.partition(':')[0].strip() in names),
        len(lines),
    )
    stray = [
        line
        for line in lines[first:]
        if line.strip() and line.partition(':')[0].strip() not in names
    ]

    assert stray == [], (
        f'期望 {cls.__qualname__}.execute 的参数描述每条一行；实际这些行会被反射丢掉：{stray}。'
        ' 把说明挪进参数之前的摘要段，参数行只留一句。'
    )
