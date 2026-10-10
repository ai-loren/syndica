"""脚本化的 OpenAI 兼容端点（只用标准库）：E2E 起真的 `syndica up`，模型指向它，不需要 key、不花钱。

按最近一条用户消息里的关键词决定这一轮做什么；工具结果回来之后给一句最终回答。`python fake_openai.py
<port>` 起服务，`GET /calls` 交回收到过的请求条数（测试据此等待）。

环境变量 `FAKE_OPENAI_SCRIPT` 指向 JSON 剧本时（几份用 `os.pathsep` 隔开，规则按先后接起来），
先按剧本作答，没有规则命中再走上面的关键词。剧本是 `{"rules": [规则, ...]}`，按顺序取第一条命中
的规则。一条规则的条件全部可选、全部要满足：

- `system`：系统提示词里含这段文字（区分 Agent 角色与验收器这类伴生调用）；
- `when`：某条用户消息含这段文字。第几步 = 最近一条这样的用户消息之后已有几条 assistant 消息；
- `seen`：请求里任意一条消息含这段文字；
- `tool`：请求里提供了这个工具。

`steps` 是逐步的回答（`reply` 等于只有一步）；步数用完这条规则就不再命中。一个回答是
`{"content": 文本, "tool_calls": [{"name": 工具名, "arguments": {...}}]}`，或
`{"rubrics": [{"id", "statement", "quote", "kind"}]}`：后者替投影器写出 JSON，锚点的 locator
取任务里原文含 `quote` 的那个来源（会话里的 locator 带随机的消息 id，剧本写不死它）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

_CALLS = {'count': 0}
_LOCK = threading.Lock()
_SCRIPT: list[dict[str, Any]] = []


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


def _completion(message: dict[str, Any], index: int) -> dict[str, Any]:
    calls = bool(message.get('tool_calls'))
    return {
        'id': f'fake-{index}',
        'object': 'chat.completion',
        'choices': [
            {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}
        ],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    }


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
        elif '终端' in user and 'Terminal' in tools:
            command = {'action': 'create', 'command': 'echo mirror-ok; sleep 30', 'wait': 0.5}
            calls = [_call('Terminal', command, index)]
        elif '后台' in user and 'Bash' in tools:
            calls = [_call('Bash', {'command': 'sleep 30', 'run_in_background': True}, index)]
        elif '派活' in user and 'Delegate' in tools:
            calls = [_call('Delegate', {'agent': 'Worker', 'query': '回一句话'}, index)]
        else:
            content = f'收到：{trailing[-1][:80] if trailing else ""}'
    message: dict[str, Any] = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return _completion(message, index)


def _step(rule: dict[str, Any], messages: list[dict[str, Any]]) -> int | None:
    """这条规则此刻走到第几步；条件不满足时 None。"""
    system = '\n'.join(_text(m) for m in messages if m.get('role') == 'system')
    if 'system' in rule and str(rule['system']) not in system:
        return None
    if 'seen' in rule and not any(str(rule['seen']) in _text(m) for m in messages):
        return None
    if 'when' not in rule:
        return sum(1 for m in messages if m.get('role') == 'assistant')
    anchor = None
    for position, message in enumerate(messages):
        if message.get('role') == 'user' and str(rule['when']) in _text(message):
            anchor = position
    if anchor is None:
        return None
    return sum(1 for m in messages[anchor + 1 :] if m.get('role') == 'assistant')


def _sources(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """投影器任务里「- locator：原文」那几行。"""
    found: list[tuple[str, str]] = []
    for message in messages:
        if message.get('role') != 'user':
            continue
        for line in _text(message).splitlines():
            if line.startswith('- ') and '：' in line:
                locator, _, text = line[2:].partition('：')
                found.append((locator.strip(), text))
    return found


def _rubrics(items: list[dict[str, Any]], messages: list[dict[str, Any]]) -> str:
    sources = _sources(messages)
    rubrics: list[dict[str, Any]] = []
    for item in items:
        quote = str(item['quote'])
        locator = next((loc for loc, text in sources if quote in text), 'msg:missing')
        rubrics.append(
            {
                'id': item['id'],
                'statement': item['statement'],
                'kind': item.get('kind', 'checkable'),
                'mandatory': True,
                'anchors': [{'source': 'user_message', 'locator': locator, 'quote': quote}],
            }
        )
    return json.dumps({'rubrics': rubrics}, ensure_ascii=False)


def scripted(body: dict[str, Any], index: int) -> dict[str, Any] | None:
    messages: list[dict[str, Any]] = body.get('messages', [])
    tools = {tool['function']['name'] for tool in body.get('tools', []) if 'function' in tool}
    for rule in _SCRIPT:
        if 'tool' in rule and str(rule['tool']) not in tools:
            continue
        step = _step(rule, messages)
        steps = cast(list[dict[str, Any]], rule['steps'] if 'steps' in rule else [rule['reply']])
        if step is None or step >= len(steps):
            continue
        answer = steps[step]
        message: dict[str, Any] = {'role': 'assistant', 'content': answer.get('content')}
        if 'rubrics' in answer:
            message['content'] = _rubrics(answer['rubrics'], messages)
        calls = [
            {
                'id': f'call-{index}-{n}',
                'type': 'function',
                'function': {
                    'name': call['name'],
                    'arguments': json.dumps(call.get('arguments', {}), ensure_ascii=False),
                },
            }
            for n, call in enumerate(answer.get('tool_calls', []))
        ]
        if calls:
            message['tool_calls'] = calls
        return _completion(message, index)
    return None


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
        self._send(scripted(body, index) or reply(body, index))


def main() -> None:
    for script in filter(None, os.environ.get('FAKE_OPENAI_SCRIPT', '').split(os.pathsep)):
        with open(script, encoding='utf-8') as stream:
            _SCRIPT.extend(json.load(stream)['rules'])
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
