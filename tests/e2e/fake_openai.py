"""脚本化的 OpenAI 兼容端点（只用标准库）：E2E 起真的 `syndica up`，模型指向它，不需要 key、不花钱。

按最近一条用户消息里的关键词决定这一轮做什么；工具结果回来之后给一句最终回答。`python fake_openai.py
<port>` 起服务，`GET /calls` 交回收到过的请求条数（测试据此等待）。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_CALLS = {'count': 0}
_LOCK = threading.Lock()


def _call(name: str, arguments: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        'id': f'call-{index}',
        'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)},
    }


def _text(message: dict[str, Any]) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
    return ''


def reply(body: dict[str, Any], index: int) -> dict[str, Any]:
    messages: list[dict[str, Any]] = body.get('messages', [])
    tools = {tool['function']['name'] for tool in body.get('tools', []) if 'function' in tool}
    last = messages[-1] if messages else {}
    if last.get('role') == 'tool':
        content: str | None = f'完成。工具说：{_text(last)[:60]}'
        calls: list[dict[str, Any]] = []
    else:
        # 内核把预算、目标这类署名块也作为 user 块追加在人的消息后面：看上一轮之后的全部 user 块。
        trailing: list[str] = []
        for message in reversed(messages):
            if message.get('role') != 'user':
                break
            trailing.append(_text(message))
        user = '\n'.join(reversed(trailing))
        content, calls = None, []
        if '确认' in user and 'Bash' in tools:
            calls = [_call('Bash', {'command': 'cat /etc/hosts'}, index)]
        elif '问我' in user and 'AskUserQuestion' in tools:
            calls = [
                _call(
                    'AskUserQuestion', {'question': '选哪个颜色？', 'options': ['红', '蓝']}, index
                )
            ]
        elif '写文件' in user and 'Write' in tools:
            report = f'# 报告\n\n第 {index} 次写下的结论。\n'
            calls = [_call('Write', {'path': 'report.md', 'content': report}, index)]
        elif '网页' in user and 'Write' in tools:
            page = '<p>hi</p><script>fetch("/api/v1/sessions?x=pwned")</script>\n'
            calls = [_call('Write', {'path': 'page.html', 'content': page}, index)]
        elif '派活' in user and 'Delegate' in tools:
            calls = [_call('Delegate', {'agent': 'Worker', 'query': '回一句话'}, index)]
        else:
            content = f'收到：{trailing[-1][:80] if trailing else ""}'
    message: dict[str, Any] = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return {
        'id': f'fake-{index}',
        'object': 'chat.completion',
        'choices': [
            {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}
        ],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send({'calls': _CALLS['count']})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        with _LOCK:
            _CALLS['count'] += 1
            index = _CALLS['count']
        self._send(reply(body, index))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
