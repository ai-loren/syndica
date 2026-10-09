"""凭证与请求守卫。

凭证只在 server 的内存里：一次性登录码由 `syndica up` 打印在人自己的终端，浏览器拿它换会话令牌，
令牌存在按源隔离的 `localStorage`、经 `Authorization: Bearer` 发送。不用 cookie：回环地址上的
cookie 不按端口隔离，Agent 起的任何本机预览服务都会收到它。
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable

from syndica.server.errors import ApiError

__all__ = ['LOGIN_TTL_S', 'SECURITY_HEADERS', 'Credentials', 'check_host', 'check_origin']

LOGIN_TTL_S = 120.0

SECURITY_HEADERS: dict[str, str] = {
    # 终端组件往页面里注入 <style>，所以 style-src 另放行内联；脚本只认本源的文件。
    'Content-Security-Policy': (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store',
}


class Credentials:
    """一次性登录码与会话令牌。比较一律常量时间。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._codes: dict[str, float] = {}
        self._tokens: list[str] = []

    def issue_code(self) -> str:
        code = secrets.token_urlsafe(32)
        self._codes[code] = self._clock() + LOGIN_TTL_S
        return code

    def exchange(self, code: object) -> str:
        """登录码换令牌。码只能换一次，过期作废；失败不区分「没有」与「过期」。"""
        now = self._clock()
        matched = None
        if isinstance(code, str):
            for known, deadline in list(self._codes.items()):
                if deadline <= now:
                    del self._codes[known]
                elif hmac.compare_digest(known.encode(), code.encode()):
                    matched = known
        if matched is None:
            raise ApiError(
                401,
                expected='syndica up 刚打印的、两分钟内没用过的登录码',
                received='登录码不对、已经用过或已经过期',
                next_action='回到终端重新运行 syndica down 与 syndica up，用新打印的链接打开',
            )
        del self._codes[matched]
        token = secrets.token_urlsafe(32)
        self._tokens.append(token)
        return token

    def issue_api_token(self) -> str:
        """机器客户端的 Bearer 令牌。与浏览器换来的令牌同一套校验，只活在这个进程的内存里。

        调用方只该把它打印给启动 server 的那个人一次。写进状态文件或配置，重启之后它仍然
        失效，但磁盘上已经留下一份能调用本机接口的凭证。
        """
        token = secrets.token_urlsafe(32)
        self._tokens.append(token)
        return token

    def valid(self, token: object) -> bool:
        if not isinstance(token, str) or not token:
            return False
        return any(hmac.compare_digest(known.encode(), token.encode()) for known in self._tokens)

    def require(self, authorization: str | None) -> None:
        scheme, _, token = (authorization or '').partition(' ')
        if scheme.lower() != 'bearer' or not self.valid(token.strip()):
            raise ApiError(
                401,
                expected='请求带 Authorization: Bearer <登录后拿到的令牌>',
                received='没有令牌，或令牌不是这个 server 进程发的（server 重启后旧令牌作废）',
                next_action=(
                    '在终端运行 syndica down 再 syndica up，用新打印的登录链接打开页面'
                    '（server 在跑时 syndica up 不再发新链接）'
                ),
            )


def _allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f'127.0.0.1:{port}', f'localhost:{port}'})


def check_host(host: str | None, port: int) -> None:
    """DNS rebinding：浏览器以为在访问别的域名时，Host 头会是那个域名。"""
    if host not in _allowed_hosts(port):
        raise ApiError(
            421,
            expected=f'Host 是 127.0.0.1:{port} 或 localhost:{port}',
            received=f'Host={host!r}',
            next_action=f'用 http://127.0.0.1:{port}/ 打开；server 只服务本机回环地址',
        )


def check_origin(origin: str | None, port: int) -> None:
    """带 Origin 的请求必须同源；不带的（非浏览器客户端）照样要令牌。"""
    if origin is None:
        return
    if origin not in {f'http://{host}' for host in _allowed_hosts(port)}:
        raise ApiError(
            403,
            expected=f'Origin 是 http://127.0.0.1:{port} 或 http://localhost:{port}',
            received=f'Origin={origin!r}',
            next_action='别的页面不能代你调用这个 server；从 syndica up 打印的链接打开页面再操作',
        )
