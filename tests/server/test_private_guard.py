"""server 的处理器不碰内核私有：除 `self` / `cls` 之外不出现 `._x` 属性访问，不 import `_x`，不拿
字面量 `_x` 去 getattr。会话装配里要读的私有入口在 CLI 侧（组合根注入），server 一处都没有。"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2] / 'src' / 'syndica' / 'server'


def _private(name: str) -> bool:
    return name.startswith('_') and not (name.startswith('__') and name.endswith('__'))


def _violations(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and _private(node.attr):
            owner = node.value
            if not (isinstance(owner, ast.Name) and owner.id in ('self', 'cls')):
                found.append(f'attribute {node.attr}')
        elif isinstance(node, ast.ImportFrom) and (node.module or '').startswith('syndica'):
            found.extend(f'import {alias.name}' for alias in node.names if _private(alias.name))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ('getattr', 'setattr', 'hasattr')
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and _private(node.args[1].value)
        ):
            found.append(f'{node.func.id} {node.args[1].value}')
    return found


def _scanned() -> dict[str, list[str]]:
    return {
        path.relative_to(_SERVER).as_posix(): _violations(path.read_text(encoding='utf-8'))
        for path in sorted(_SERVER.rglob('*.py'))
    }


def test_no_server_module_reaches_into_private_names():
    assert {name: found for name, found in _scanned().items() if found} == {}


def test_the_scan_covers_the_handlers_and_every_server_module():
    walked = sorted(
        (Path(root) / name).relative_to(_SERVER).as_posix()
        for root, _, names in os.walk(_SERVER)
        for name in names
        if name.endswith('.py')
    )
    assert 'app.py' in _scanned()
    assert sorted(_scanned()) == walked


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('runtime._budget_of("a")', ['attribute _budget_of']),
        ('from syndica.kernel.stores.file_session import _valid_sid', ['import _valid_sid']),
        ('getattr(runtime, "_table")', ['getattr _table']),
        ('self._x = 1\ncls._y\nobj.__class__', []),
    ],
)
def test_each_rule_catches_its_form(source: str, expected: list[str]):
    assert _violations(source) == expected
