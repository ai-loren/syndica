"""trace 的 L2 端到端（M3 T17）：经公开 CLI、用按 Agent 分发的脚本化模型，在夹具项目的 tmp
副本里跑。

夹具在 `tests/fixtures/trace/project/`，每个文件带 `.fixture` 后缀（pytest、ruff、pyright 与链接
检查都不把它当成仓库内容），还原时去掉。假凭证全部在运行时拼出来。宿主的环境只有跑命令要的那几项与
一个空的 HOME：开发者 shell 里名字像密钥的变量会成为脱敏器的密钥值，HOME 里的 git 配置会被宿主的
`git rev-parse` 读到。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import string
import threading
from collections.abc import Callable, Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import syndica.__main__ as entry
from syndica.cli.handoff import trace_directory
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    BaseAgent,
    ContextBlock,
    EdgeKind,
    FileTraceStore,
    LLMConfig,
    LLMResponseError,
    PayloadRef,
    RuleRedactor,
    Span,
    SpanKind,
    SpanStatus,
    ToolCall,
    TraceHeader,
    TraceLevel,
)
from syndica.kernel.redaction import encode_redacted
from syndica.kernel.stores import file_trace
from syndica.kernel.tool_executor import ToolExecutor
from syndica.kernel.tracing import current_llm_call

_FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'trace' / 'project'
_NOT_GIVEN: Any = object()

Step = ContextBlock | Callable[[Sequence[str]], ContextBlock] | BaseException


def _fixture_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(_FIXTURE.rglob('*')):
        if path.is_file():
            digest.update(str(path.relative_to(_FIXTURE)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(autouse=True)
def _fixture_untouched() -> Iterator[None]:
    before = _fixture_digest()
    yield
    assert _fixture_digest() == before


def _restored(tmp_path: Path) -> Path:
    root = tmp_path / 'project'
    for source in sorted(_FIXTURE.rglob('*.fixture')):
        target = root / source.relative_to(_FIXTURE).with_suffix('')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


def _calls(*calls: tuple[str, str, dict[str, object]]) -> ContextBlock:
    return ContextBlock(
        role='assistant',
        tool_calls=[
            ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))
            for call_id, name, arguments in calls
        ],
    )


def _say(text: str) -> ContextBlock:
    return ContextBlock(role='assistant', content=text)


class _PerAgent:
    """按 Agent 给回复：第一个来要回复的是根，第二个是它派出的子 Agent。一步可以是函数，按此刻已经
    知道的 Agent id 现写参数；也可以是一个异常，那一次模型调用就抛它。脚本之外多要的一次记进
    `overruns`：它在内核看来是一次模型失败，不记下来就会被当成用例想要的那次失败。"""

    def __init__(self, *scripts: list[Step]) -> None:
        self.config = LLMConfig(provider='test', model='per-agent')
        self._scripts = scripts
        self._agents: list[str] = []
        self._next: dict[str, int] = {}
        self.overruns: list[tuple[int, int]] = []

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        key = current_llm_call()
        assert key is not None
        agent_id = key[0]
        if agent_id not in self._agents:
            self._agents.append(agent_id)
        position = self._next.get(agent_id, 0)
        self._next[agent_id] = position + 1
        number = self._agents.index(agent_id)
        script = self._scripts[number]
        if position >= len(script):
            self.overruns.append((number, position))
            raise AssertionError(
                f'Agent #{number} 要第 {position + 1} 次回复，脚本只有 {len(script)}'
            )
        step = script[position]
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, ContextBlock) else step(self._agents)


def _environ(root: Path, **extra: str) -> dict[str, str]:
    home = root.parent / 'home'
    home.mkdir(exist_ok=True)
    base = {
        key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ
    }
    return {**base, 'HOME': str(home), **extra}


def _cli(
    root: Path,
    llm: Any,
    argv: list[str],
    *,
    stdin: str = '',
    environ: dict[str, str] | None = None,
    replayer: Any = _NOT_GIVEN,
) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=root,
        environ=_environ(root) if environ is None else environ,
        replayer=entry._replay_document if replayer is _NOT_GIVEN else replayer,  # pyright: ignore[reportPrivateUsage]
        exporter=entry._export_trace,  # pyright: ignore[reportPrivateUsage]
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close(interrupted=False)
    return code, stdout.getvalue(), stderr.getvalue()


def _store(root: Path) -> FileTraceStore:
    return FileTraceStore(trace_directory(root))


def _headers(root: Path) -> list[TraceHeader]:
    return asyncio.run(_store(root).traces())


def _spans(root: Path, trace_id: str) -> list[Span]:
    return asyncio.run(_store(root).spans(trace_id))


def _payload(root: Path, trace_id: str, digest: object) -> dict[str, Any]:
    assert isinstance(digest, str)
    text = asyncio.run(_store(root).payload(PayloadRef(trace_id, digest)))
    assert text is not None
    return json.loads(text)


def _section(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(title) + 1
    end = next((index for index in range(start, len(lines)) if not lines[index]), len(lines))
    return lines[start:end]


# --- a delegating run, shown --------------------------------------------------------------


def test_a_delegating_run_answers_the_three_questions_and_records_both_kinds_of_edge(
    tmp_path: Path,
):
    root = _restored(tmp_path)
    lead: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Explorer', 'query': 'read the notes'})),
        _calls(('b1', 'Bash', {'command': 'sleep 0.3'}), ('r2', 'Read', {'path': 'todo.txt'})),
        _say('all done'),
    ]
    child: list[Step] = [
        _calls(('r1', 'Read', {'path': 'notes.md'})),
        lambda agents: _calls(
            ('s1', 'SendMessage', {'agent_id': agents[0], 'content': 'the notes are short'})
        ),
        _say('notes read'),
    ]
    llm = _PerAgent(lead, child)

    code, out, err = _cli(root, llm, ['run', 'go', '--agent', 'Worker'])

    assert (code, llm.overruns) == (0, []), err
    assert out.rstrip().endswith('all done')
    [header] = _headers(root)
    spans = _spans(root, header.trace_id)
    agents = {span.agent_id: span for span in spans if span.kind is SpanKind.AGENT}
    [lead_id] = [agent_id for agent_id, span in agents.items() if span.name == 'Worker']
    [child_id] = [agent_id for agent_id, span in agents.items() if span.name == 'Explorer']
    [delegate] = [span for span in spans if span.name == 'Delegate']
    [send] = [span for span in spans if span.name == 'SendMessage']
    edges = asyncio.run(_store(root).edges(header.trace_id))
    [spawn] = [edge for edge in edges if edge.kind is EdgeKind.SPAWN]
    [message] = [edge for edge in edges if edge.kind is EdgeKind.MESSAGE]
    assert (spawn.source_span_id, spawn.target_span_id) == (
        delegate.span_id,
        agents[child_id].span_id,
    )
    assert spawn.attributes['child.agent_id'] == child_id
    leads_own = {
        span.span_id
        for span in spans
        if span.agent_id == lead_id and span.kind in (SpanKind.TURN, SpanKind.WAIT)
    }
    assert message.source_span_id == send.span_id and message.target_span_id in leads_own
    assert 'message.id' in message.attributes

    code, shown, _ = _cli(root, None, ['trace', 'show', header.trace_id[:8]])

    assert code == 0
    tree = _section(shown, '谁创建了谁：')
    assert tree[0] == f'  Worker {lead_id[:8]}'
    assert tree[1].startswith(f'     └─ Explorer {child_id[:8]}，创建于 +')
    [chain] = _section(shown, '谁在等谁：')
    assert chain.startswith(f'  Worker {lead_id[:8]} → Explorer {child_id[:8]}（')
    assert chain.endswith('，answered）')
    slowest = _section(shown, '最慢的工具调用（前 3 个，等别的 Agent 与人的时间不算）：')
    assert slowest[0].split('  ')[2:4] == [f'Worker {lead_id[:8]}', 'Bash']


# --- the agent cannot read the host's records ---------------------------------------------


def test_the_agent_cannot_read_or_write_the_hosts_records_with_its_file_tools(tmp_path: Path):
    root = _restored(tmp_path)
    assert _cli(root, _PerAgent([_say('first')]), ['run', 'first', '--agent', 'Worker'])[0] == 0
    [first] = _headers(root)
    [directory] = list(trace_directory(root).iterdir())
    target = f'.syndica/traces/{directory.name}/trace.json'
    before = (root / target).read_bytes()
    steps: list[Step] = [
        _calls(
            ('r1', 'Read', {'path': target}),
            ('r2', 'Read', {'path': target.upper()}),
            ('r3', 'Read', {'path': '.syndica/handoffs/latest.md'}),
            ('w1', 'Write', {'path': target, 'content': '{}'}),
        ),
        _say('could not touch them'),
    ]
    llm = _PerAgent(steps)

    code, out, err = _cli(root, llm, ['run', 'go', '--agent', 'Worker'])

    assert (code, llm.overruns) == (0, []), err
    assert out.rstrip().endswith('could not touch them')
    assert (root / target).read_bytes() == before
    [newest] = [header for header in _headers(root) if header.trace_id != first.trace_id]
    outputs = {
        span.attributes['tool.call_id']: _payload(
            root, newest.trace_id, span.attributes['tool.result']
        )['output']
        for span in _spans(root, newest.trace_id)
        if span.kind is SpanKind.TOOL_CALL
    }
    assert set(outputs) == {'r1', 'r2', 'r3', 'w1'}
    assert all(
        output.startswith('期望: 文件工具的路径不在宿主保留的目录里') for output in outputs.values()
    )
    assert {call: '.syndica/handoffs 里' in output for call, output in outputs.items()} == {
        'r1': False,
        'r2': False,
        'r3': True,
        'w1': False,
    }


# --- a failed run, replayed; and a kernel that changed ------------------------------------


def _failing_run(root: Path) -> TraceHeader:
    failure = LLMResponseError(expected='a reply', received='HTTP 503', next_action='retry later')
    llm = _PerAgent([_calls(('c1', 'Bash', {'command': 'printf hi'})), failure])
    code, _, err = _cli(root, llm, ['run', 'go', '--agent', 'Worker'])
    assert (code, llm.overruns) == (1, []), err
    [header] = _headers(root)
    assert header.status is SpanStatus.ERROR
    return header


def test_a_failed_run_is_replayed_to_the_same_failure(tmp_path: Path):
    root = _restored(tmp_path)
    original = _failing_run(root)
    results: list[Any] = []

    def capturing(document: Any, **options: Any) -> Any:
        result = entry._replay_document(document, **options)  # pyright: ignore[reportPrivateUsage]
        results.append(result)
        return result

    code, out, err = _cli(
        root, None, ['trace', 'replay', original.trace_id[:8]], replayer=capturing
    )

    assert code == 0, err
    assert out.startswith(f'复现：trace {original.trace_id} 的回放与记录逐项相同。')
    [result] = results
    assert 'HTTP 503' in result.result.errors['Worker[0]']


def test_a_kernel_that_now_behaves_differently_is_caught_at_the_unit_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _restored(tmp_path)
    original = _failing_run(root)
    append = ToolExecutor._append  # pyright: ignore[reportPrivateUsage]

    def louder(self: ToolExecutor, ctx: Any, call: ToolCall, result: Any) -> None:
        append(
            self, ctx, call, type(result)(output=f'{result.output} (and more)', trust=result.trust)
        )

    monkeypatch.setattr(ToolExecutor, '_append', louder)

    code, out, _ = _cli(root, None, ['trace', 'replay', original.trace_id[:8]])

    assert code == 1
    lines = out.splitlines()
    assert lines[0] == f'分叉：trace {original.trace_id} 的回放有 1 处与记录不同：'
    assert lines[1] == '  request  Worker[0] 第 2 轮第 1 次调用：请求的「第 2 条历史」与记录不同'


def test_a_kernel_that_now_recovers_from_the_failure_is_caught_by_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _restored(tmp_path)
    original = _failing_run(root)

    async def recovered(self: BaseAgent, ctx: Any, request: Any, error: Exception) -> ContextBlock:
        return ContextBlock(role='assistant', content='recovered')

    monkeypatch.setattr(BaseAgent, '_substituted', recovered)

    code, out, _ = _cli(root, None, ['trace', 'replay', original.trace_id[:8]])

    assert code == 1
    assert "  outcome  expect.stop_reasons：记录 {'Worker[0]': 'error'}" in out


# --- export, and a store that fails -------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.received: list[tuple[str, str, dict[str, Any]]] = []
        holder = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers['Content-Length'])
                body = json.loads(self.rfile.read(length))
                path = self.requestline.split()[1]
                holder.received.append((path, self.headers['Content-Type'], body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.address = f'http://127.0.0.1:{self._server.server_address[1]}'
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def collector() -> Iterator[_Collector]:
    running = _Collector()
    try:
        yield running
    finally:
        running.close()


def test_a_run_is_exported_to_a_collector_as_the_recorder_wrote_it(
    tmp_path: Path, collector: _Collector
):
    root = _restored(tmp_path)
    forged = Span(
        span_id='f0' * 8,
        parent_span_id=None,
        kind=SpanKind.TURN,
        name='forged',
        agent_id='f1' * 16,
        start_ns=1,
        end_ns=2,
        status=SpanStatus.OK,
        attributes={'turn': 1},
    )
    line = encode_redacted(file_trace._span_value(forged), RuleRedactor())  # pyright: ignore[reportPrivateUsage]
    # 重定向的目标在 POSIX sh 里不展开通配符：逐个文件写。第二轮才写：那时第一轮的 span 已经落盘。
    plant = (
        'for f in .syndica/traces/*/spans.jsonl; do '
        f'printf \'%s\\n\' {shlex.quote(line)} >> "$f"; done'
    )
    steps: list[Step] = [
        _calls(('r1', 'Read', {'path': 'todo.txt'})),
        _calls(('b1', 'Bash', {'command': plant})),
        _say('two items'),
    ]
    llm = _PerAgent(steps)

    code, _, err = _cli(
        root,
        llm,
        ['run', 'go', '--agent', 'Worker'],
        environ=_environ(root, OTEL_EXPORTER_OTLP_ENDPOINT=collector.address),
    )

    assert (code, llm.overruns) == (0, []), err
    [header] = _headers(root)
    stored = [span.span_id for span in _spans(root, header.trace_id)]
    assert forged.span_id in stored
    [(path, content_type, document)] = collector.received
    assert (path, content_type) == ('/v1/traces', 'application/json')
    sent = document['resourceSpans'][0]['scopeSpans'][0]['spans']
    assert [span['spanId'] for span in sent] == [span for span in stored if span != forged.span_id]
    assert {span['traceId'] for span in sent} == {header.trace_id}
    assert all(re.fullmatch(r'[0-9]+', span['startTimeUnixNano']) for span in sent)


def test_a_trace_exported_later_is_redacted_with_the_secrets_known_then(
    tmp_path: Path, collector: _Collector
):
    root = _restored(tmp_path)
    token = ''.join(random.Random(23).choices(string.ascii_letters + string.digits, k=28))
    llm = _PerAgent([_say('noted')])
    code, _, err = _cli(root, llm, ['run', f'remember {token}', '--agent', 'Worker'])
    assert (code, llm.overruns) == (0, []), err
    [header] = _headers(root)

    code, _, err = _cli(
        root,
        None,
        ['trace', 'export', header.trace_id[:8], '--otlp', collector.address],
        environ=_environ(root, LATER_TOKEN=token),
    )

    assert code == 0, err
    [(_, _, document)] = collector.received
    assert token not in json.dumps(document)
    assert '[REDACTED:secret.env_value]' in json.dumps(document)


def test_a_trace_that_cannot_be_written_leaves_the_run_alone_with_one_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector: _Collector
):
    root = _restored(tmp_path)

    async def full(self: FileTraceStore, trace_id: str, spans: Any = (), edges: Any = ()) -> None:
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(FileTraceStore, 'append', full)
    llm = _PerAgent([_calls(('r1', 'Read', {'path': 'todo.txt'})), _say('two items')])

    code, out, err = _cli(
        root,
        llm,
        ['run', 'go', '--agent', 'Worker'],
        environ=_environ(root, OTEL_EXPORTER_OTLP_ENDPOINT=collector.address),
    )

    assert (code, llm.overruns) == (0, [])
    assert out.rstrip().splitlines()[-1] == 'two items'
    [directory] = list(trace_directory(root).iterdir())
    lines = err.splitlines()
    assert len(lines) == 3, err
    assert lines[0].startswith('期望: trace ') and 'No space left on device' in lines[0]
    assert lines[1] == (
        f'trace（不完整，原因见上面的 ERROR；不要拿它回放）：{directory.relative_to(root)}'
    )
    assert lines[2] == '不完整的 trace 不导出（原因见上面的 ERROR）。'
    assert collector.received == []


# --- every channel, every rule family -----------------------------------------------------


def _fake_secrets(seed: int) -> dict[str, str]:
    """每个通道一个不同的假凭证，各命中一族规则；全部在运行时拼出来。名字带 `_env` 的只有宿主的
    脱敏器认得（它们是宿主环境里的值）：store 自己的编码只认内置规则的形状，记录器漏掉它们时
    没有第二道替它兜着。"""
    rng = random.Random(seed)

    def pick(alphabet: str, count: int) -> str:
        return ''.join(rng.choice(alphabet) for _ in range(count))

    alnum = string.ascii_letters + string.digits
    return {
        'prompt': 'sk-' + pick(alnum, 45) + 'Q7x',
        'prompt_env': pick(alnum, 25) + 'p1A',
        'call_id_env': pick(alnum, 25) + 'c2B',
        'confirmation_env': pick(alnum, 25) + 'k3C',
        'file_env': pick(alnum, 25) + 'h4J',
        'tool_args': pick(alnum, 21) + 'r5T',
        'tool_output': pick(alnum, 21) + 'm3W',
        'key_block': '\n'.join(pick(alnum + '+/', 64) for _ in range(3)),
        'command_output': 'AKIA' + pick(string.ascii_uppercase + string.digits, 16),
        'reply': 'sk-or-v1-' + pick('0123456789abcdef', 64),
        'child_prompt': 'AIza' + pick(alnum, 35),
        'question': 'xoxb-' + pick(string.digits, 10) + '-' + pick(alnum, 24),
        'answer': 'ghp_' + pick(alnum, 36),
        'provider_error': pick(alnum, 29) + 'b2N',
        'custom': 'corp-' + pick(string.ascii_lowercase + string.digits, 12) + '.internal',
    }


_PLACEHOLDERS = {
    'secret.openai_key',
    'secret.env_value',
    'secret.url_userinfo',
    'secret.named_assignment',
    'secret.private_key_block',
    'secret.aws_access_key',
    'secret.openrouter_key',
    'secret.google_api_key',
    'secret.slack_token',
    'secret.github_token',
    'secret.authorization_header',
    'custom.internal_host',
}


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _decoded(directory: Path) -> list[str]:
    """trace 目录里每个文件的原文，与解码之后的每一个字符串。"""
    found: list[str] = []
    for path in sorted(directory.rglob('*')):
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        found.append(text)
        documents = text.splitlines() if path.suffix == '.jsonl' else [text]
        for document in documents:
            found.extend(_strings(json.loads(document)))
    return found


@pytest.mark.parametrize('level', ['replay', 'full'])
def test_no_secret_from_any_channel_survives_anywhere_in_the_trace_directory(
    tmp_path: Path, level: str
):
    root = _restored(tmp_path)
    secrets = _fake_secrets(17)
    begin, end = '-----BEGIN RSA PRIVATE KEY-----', '-----END RSA PRIVATE KEY-----'
    (root / 'deploy.env').write_text(
        f'password={secrets["tool_output"]}\n{begin}\n{secrets["key_block"]}\n{end}\n'
        f'service at {secrets["file_env"]}\n',
        encoding='utf-8',
    )
    (root / 'aws.txt').write_text(f'{secrets["command_output"]}\n', encoding='utf-8')
    (root / '.syndica.yaml').write_text(
        'version: 1\nredaction:\n  rules:\n'
        "    - {id: custom.internal_host, pattern: 'corp-[a-z0-9]+\\.internal'}\n",
        encoding='utf-8',
    )
    call_id = f'call_{secrets["call_id_env"]}'
    # 确认放在最前面：读过文件之后（输出不可信）同样的命令直接被拦下，等不到人。
    lead: list[Step] = [
        _calls(
            (
                'c1',
                'Bash',
                {'command': f'cat /etc/hosts >/dev/null; true {secrets["confirmation_env"]}'},
            )
        ),
        _calls(
            ('r1', 'Read', {'path': 'deploy.env'}),
            (
                call_id,
                'Bash',
                {
                    'command': f'cat aws.txt; true https://deploy:{secrets["tool_args"]}@git.example/x'
                },
            ),
        ),
        _calls(('q1', 'AskUser', {'question': f'paste the token for {secrets["question"]}'})),
        _calls(
            ('d1', 'Delegate', {'agent': 'Explorer', 'query': f'check {secrets["child_prompt"]}'})
        ),
        _say(f'done; the router key is {secrets["reply"]} at {secrets["custom"]}'),
    ]
    child: list[Step] = [
        LLMResponseError(
            expected='a reply',
            received=f'HTTP 401 for Authorization: Bearer {secrets["provider_error"]}',
            next_action='check the key',
        )
    ]
    llm = _PerAgent(lead, child)
    environ = _environ(
        root,
        DEPLOY_TOKEN=secrets['file_env'] + '\n',
        ARGV_TOKEN=secrets['prompt_env'],
        CALL_TOKEN=secrets['call_id_env'],
        CONFIRM_TOKEN=secrets['confirmation_env'],
    )
    argv = ['run', f'deploy with {secrets["prompt"]} {secrets["prompt_env"]}', '--agent', 'Worker']

    code, _, err = _cli(
        root,
        llm,
        [*argv, *([] if level == 'replay' else ['--trace', level])],
        stdin=f'approve\n{secrets["answer"]}\n',
        environ=environ,
    )

    assert (code, llm.overruns) == (0, []), err
    [header] = _headers(root)
    assert header.level is TraceLevel(level)
    spans = _spans(root, header.trace_id)
    tools = {
        span.attributes['tool.call_id']: span for span in spans if span.kind is SpanKind.TOOL_CALL
    }
    assert set(tools) == {'r1', 'call_[REDACTED:secret.env_value]', 'c1', 'q1', 'd1'}
    bash = tools['call_[REDACTED:secret.env_value]']
    assert bash.attributes['tool.outcome'] == 'ok'
    assert (
        '[REDACTED:secret.aws_access_key]'
        in _payload(root, header.trace_id, bash.attributes['tool.result'])['output']
    )
    assert tools['c1'].attributes['danger.disposition'] == 'confirm'
    if level == 'full':
        assert any('llm.request' in span.attributes for span in spans)
    [directory] = list(trace_directory(root).iterdir())
    strings = _decoded(directory)
    for channel, secret in secrets.items():
        for piece in secret.splitlines():
            assert all(piece not in text for text in strings), channel
            fragments = {piece[i : i + 16] for i in range(max(len(piece) - 15, 0))}
            assert all(fragment not in text for text in strings for fragment in fragments), channel
    joined = '\n'.join(strings)
    assert [rule for rule in sorted(_PLACEHOLDERS) if f'[REDACTED:{rule}]' not in joined] == []

    code, out, err = _cli(root, None, ['trace', 'replay', header.trace_id[:8]], environ=environ)

    assert code == 0, err
    assert out.startswith(f'复现：trace {header.trace_id} 的回放与记录逐项相同。')
