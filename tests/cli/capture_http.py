"""本机上的 HTTP 对端：记下每个请求的方法、路径与 Authorization 头，按给定的函数作答。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass(frozen=True)
class Seen:
    method: str
    path: str
    authorization: str | None


Reply = Callable[[str, str, bytes], tuple[int, object]]


@dataclass
class Capture:
    url: str
    seen: list[Seen] = field(default_factory=list[Seen])


def refuse_everything(method: str, path: str, body: bytes) -> tuple[int, object]:
    del method, path, body
    return 400, {'error': 'capture only'}


@contextmanager
def capturing(reply: Reply = refuse_everything) -> Iterator[Capture]:
    lock = threading.Lock()
    seen: list[Seen] = []

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else b''
            with lock:
                seen.append(Seen(self.command, self.path, self.headers.get('Authorization')))
            status, payload = reply(self.command, self.path, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)

        do_GET = _serve
        do_POST = _serve
        do_DELETE = _serve

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    capture = Capture(url=f'http://127.0.0.1:{server.server_address[1]}', seen=seen)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield capture
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
