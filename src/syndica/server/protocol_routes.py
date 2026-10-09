"""挂在 `syndica up` 上的协议路由。

路由表与有没有装 extra 无关：没装时交回 501 和安装指引，装了但适配还没接上会话时也交回 501，
两条的下一步不同。对方协议的报文不在这里展开。
"""

from __future__ import annotations

import importlib.util

from starlette.requests import Request
from starlette.responses import Response

from syndica.server.errors import ApiError

__all__ = ['protocol_handler']

# (路径, 方法, 是否公开, extra 名, 要 import 的模块；空 extra 是 OpenAI 兼容 HTTP，不另装包)
PROTOCOL_ROUTES: tuple[tuple[str, tuple[str, ...], bool, str, str], ...] = (
    ('/.well-known/agent-card.json', ('GET',), True, 'a2a', 'a2a'),
    ('/a2a/', ('POST',), False, 'a2a', 'a2a'),
    ('/mcp', ('GET', 'POST', 'DELETE'), False, 'mcp', 'mcp'),
    ('/v1/models', ('GET',), False, '', ''),
    ('/v1/chat/completions', ('POST',), False, '', ''),
    ('/v1/responses', ('POST',), False, '', ''),
)


def protocol_handler(extra: str, module: str) -> object:
    """交回一个会抛 `ApiError` 的处理器。适配接上会话之后，由那一层换掉这个函数的后半。"""

    async def handle(_request: Request) -> Response:
        if extra and importlib.util.find_spec(module) is None:
            raise ApiError(
                501,
                expected=f'已安装 syndica[{extra}]（提供 {module}）',
                received=f'当前环境没有 {module}',
                next_action=f"pip install 'syndica[{extra}]' 之后重启 syndica up",
            )
        installed = 'syndica up 自带，不需要额外安装' if not extra else f'syndica[{extra}] 已安装'
        raise ApiError(
            501,
            expected='这条协议路由已经接上 Syndica 的会话',
            received=f'{installed}，适配还没有挂上',
            next_action='适配接上之前这条路由只说明它在；重启 server 不会从磁盘找回机器令牌',
        )

    return handle
