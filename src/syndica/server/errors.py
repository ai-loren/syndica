"""面向调用方的错误：HTTP 体与 WebSocket 帧都是 `{"expected", "received", "next_action"}`。"""

from __future__ import annotations

from syndica.kernel import SyndicaError

__all__ = ['ApiError', 'error_body']


class ApiError(Exception):
    """处理器抛出的三件套错误；`status` 是 HTTP 状态码。"""

    def __init__(self, status: int, *, expected: str, received: str, next_action: str) -> None:
        super().__init__(f'{expected} / {received} / {next_action}')
        self.status = status
        self.expected = expected
        self.received = received
        self.next_action = next_action

    @classmethod
    def of(cls, status: int, error: SyndicaError) -> ApiError:
        return cls(
            status,
            expected=error.expected,
            received=error.received,
            next_action=error.next_action,
        )


def error_body(error: ApiError) -> dict[str, str]:
    return {
        'expected': error.expected,
        'received': error.received,
        'next_action': error.next_action,
    }
