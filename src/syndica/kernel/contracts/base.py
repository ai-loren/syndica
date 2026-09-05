"""契约套件共享的基座。

每套契约都以 `async def` 用例为主体。第三方的 async 插件缺失或配置成 strict 时，这些用例被
**静默跳过**——在汇总里与通过无法区分。契约套件是后端证明自己合规的凭据，静默通过是唯一不可
接受的结局，所以这条守卫由所有套件继承，而不是各自复制一份。
"""

from __future__ import annotations

from inspect import iscoroutinefunction
from typing import Any

__all__ = ['ContractSuite']

# 能驱动 async 用例的插件所打的标记；命中任意一个即视为该用例真的会被执行。刻意不改成读
# pytest-asyncio 的 asyncio_mode ini 值：那会把公开契约套件焊死在单一插件上，正当使用 anyio
# 的第三方会被判不合规。
_ASYNC_PLUGIN_MARKS = frozenset({'anyio', 'asyncio', 'trio'})


class ContractSuite:
    """所有 Protocol 契约套件的基类。"""

    def test_async_cases_are_driven_by_an_async_plugin(self, request: Any) -> None:
        # request 是 pytest 按名注入的 fixture，是本模块接触 pytest 配置的唯一合规途径：
        # kernel 只依赖标准库 + pydantic + 一个 HTTP 客户端，不得 import pytest。
        async_items = [
            item
            for item in request.session.items
            if getattr(item, 'cls', None) is type(self)
            and iscoroutinefunction(getattr(item, 'obj', None))
        ]
        # 标记是 collection 期打上的：没被收集的 async 用例根本没有标记可读。因此收集数为零时
        # 「读不到标记」既可能是插件缺失、也可能是 -k / -m / --deselect / --lf / xdist 分片
        # 把它们排除了，两者无从区分——没有证据就不下结论，而不是判失败。
        if not async_items:
            return
        undriven = [
            item.nodeid
            for item in async_items
            if not any(mark.name in _ASYNC_PLUGIN_MARKS for mark in item.iter_markers())
        ]
        assert not undriven, (
            f'期望每个 async 用例都带上 {sorted(_ASYNC_PLUGIN_MARKS)} 之一的标记，'
            ' 即真的有插件驱动它。'
            f' 实际有 {len(undriven)} 个没有，例如 {undriven[0]}。'
            ' 装上 pytest-asyncio 并设 asyncio_mode = "auto"，否则这些用例会被静默跳过，'
            ' 汇总里看起来与通过一模一样。'
        )
