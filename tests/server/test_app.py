"""server 的 HTTP / SSE / WebSocket 面：经真 uvicorn 与真 TCP 跑（httpx 的内存传输会把整段响应收完
才返回，测不了流）。会话装配是假的，见 `server_fakes`。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from server_fakes import SECRET, FakeAssembly, FakeHandle, running, sse_events
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.typing import Origin

from syndica.kernel import Event
from syndica.server import MAX_STREAMS, Credentials
from syndica.server.app import MAX_HANDSHAKES
from syndica.server.auth import LOGIN_TTL_S
from syndica.server.streams import RESET


async def _rpc(base: str, port: int, sid: str, token: str) -> Any:
    socket = await websockets.connect(
        f'ws://127.0.0.1:{port}/api/v1/sessions/{sid}/rpc', origin=Origin(base)
    )
    await socket.send(json.dumps({'op': 'auth', 'id': 0, 'token': token}))
    assert json.loads(await socket.recv())['ok'] is True
    return socket


async def _call(socket: Any, **frame: Any) -> dict[str, Any]:
    await socket.send(json.dumps(frame))
    return json.loads(await socket.recv())


async def test_health_is_public_and_carries_the_process_nonce():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get('/api/v1/health')
    assert response.status_code == 200
    assert response.json() == {'nonce': 'nonce-1', 'version': 'test'}


@pytest.mark.parametrize(
    'headers',
    [{}, {'authorization': 'Bearer wrong'}, {'authorization': 'Basic abc'}, {'authorization': ''}],
)
async def test_api_routes_refuse_requests_without_this_servers_token(headers: dict[str, str]):
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get('/api/v1/sessions', headers=headers)
    assert response.status_code == 401
    assert set(response.json()) == {'expected', 'received', 'next_action'}
    assert 'syndica down' in response.json()['next_action']


async def test_a_valid_token_under_another_scheme_is_refused():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get(
            '/api/v1/sessions', headers={'authorization': f'Basic {server.token}'}
        )
        accepted = await client.get(
            '/api/v1/sessions', headers={'authorization': f'Bearer {server.token}'}
        )
    assert (response.status_code, accepted.status_code) == (401, 200)


@pytest.mark.parametrize(
    'budget',
    [{'turns': True}, {'turns': '3'}, {'turns': 1.0}, {'tokens': 2.5}, {'wall_clock_s': True}],
)
async def test_a_budget_field_of_the_wrong_type_family_is_refused(budget: dict[str, Any]):
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.post(
            '/api/v1/sessions', headers=server.login(), json={'budget': budget}
        )
    assert response.status_code == 422
    assert next(iter(budget)) in response.json()['received']


@pytest.mark.parametrize('budget', [{'turns': 3, 'wall_clock_s': 30}, {'wall_clock_s': 2.5}])
async def test_a_budget_of_plain_numbers_is_accepted(budget: dict[str, Any]):
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.post(
            '/api/v1/sessions', headers=server.login(), json={'budget': budget}
        )
    assert response.status_code == 201, response.json()


@pytest.mark.parametrize('host', ['evil.example:80', 'localhost', '127.0.0.1', '127.0.0.2:{port}'])
async def test_a_request_for_another_host_name_is_refused(host: str):
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get(
            '/api/v1/health', headers={'host': host.format(port=server.port)}
        )
    assert response.status_code == 421
    assert 'Host' in response.json()['expected']


@pytest.mark.parametrize('origin', ['http://evil.example', 'http://127.0.0.1:1', 'null'])
async def test_a_request_from_another_origin_is_refused(origin: str):
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.post(
            '/api/v1/sessions', headers={**server.login(), 'origin': origin}, json={}
        )
    assert response.status_code == 403
    assert 'Origin' in response.json()['expected']


async def test_the_same_origin_is_accepted():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get(
            '/api/v1/roles', headers={**server.login(), 'origin': server.base}
        )
    assert response.status_code == 200
    assert response.json() == {'roles': ['Orchestrator', 'Worker']}


async def test_a_login_code_works_once_and_never_sets_a_cookie():
    credentials = Credentials()
    async with (
        running(FakeAssembly(), credentials=credentials) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        code = credentials.issue_code()
        first = await client.post('/api/v1/login', json={'code': code})
        second = await client.post('/api/v1/login', json={'code': code})
        created = await client.post('/api/v1/sessions', headers=server.login(), json={})
    assert first.status_code == 200
    assert credentials.valid(first.json()['token'])
    assert second.status_code == 401
    for response in (first, second, created):
        assert 'set-cookie' not in response.headers


def test_an_expired_login_code_is_refused():
    now = [0.0]
    credentials = Credentials(clock=lambda: now[0])
    code = credentials.issue_code()
    now[0] = LOGIN_TTL_S + 0.01
    with pytest.raises(Exception) as caught:
        credentials.exchange(code)
    assert getattr(caught.value, 'status', None) == 401


@pytest.mark.parametrize('code', ['', 'nope', None, 12])
def test_a_login_code_that_was_never_issued_is_refused(code: object):
    credentials = Credentials()
    credentials.issue_code()
    with pytest.raises(Exception) as caught:
        credentials.exchange(code)
    assert getattr(caught.value, 'status', None) == 401


async def test_every_response_carries_the_security_headers():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        responses = [
            await client.get('/api/v1/health'),
            await client.get('/api/v1/sessions'),
            await client.get('/'),
        ]
    for response in responses:
        policy = response.headers['content-security-policy']
        assert "frame-ancestors 'none'" in policy
        assert "script-src 'self';" in policy
        assert response.headers['x-frame-options'] == 'DENY'
        assert response.headers['x-content-type-options'] == 'nosniff'


async def test_creating_a_session_passes_the_role_and_budget_and_returns_a_snapshot():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.post(
            '/api/v1/sessions',
            headers=server.login(),
            json={'role': 'Worker', 'budget': {'turns': 5, 'cost': '0.50'}},
        )
        listed = await client.get('/api/v1/sessions', headers=server.login())
    assert response.status_code == 201
    body = response.json()
    assert body['sid'] == 's0'
    assert body['as_of'].endswith(':0')
    text = body['transcripts']['s0']['blocks'][0]['text']
    assert text.startswith('token=[REDACTED:') and SECRET not in text
    opened = assembly.opened[0]
    assert (opened['role'], opened['turns'], str(opened['cost'])) == ('Worker', 5, '0.50')
    assert [(row['sid'], row['live']) for row in listed.json()['sessions']] == [
        ('stored', False),
        ('s0', True),
    ]


@pytest.mark.parametrize(
    'body',
    [
        {'role': 3},
        {'budget': {'turns': 0}},
        {'budget': {'cost': 'lots'}},
        {'budget': {'cost': '-1'}},
        {'budget': {'tokens': 1.5}},
        {'unknown': True},
    ],
)
async def test_a_malformed_create_body_is_refused_with_three_parts(body: dict[str, Any]):
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.post('/api/v1/sessions', headers=server.login(), json=body)
    assert response.status_code == 422
    assert set(response.json()) == {'expected', 'received', 'next_action'}
    assert assembly.opened == []


async def test_events_stream_with_epoch_ids_and_redacted_payloads():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        created = (await client.post('/api/v1/sessions', headers=server.login(), json={})).json()
        handle = assembly.handles[0]
        reader = asyncio.create_task(
            sse_events(
                client,
                '/api/v1/sessions/s0/events',
                server.login(),
                3,
                last_event_id=created['as_of'],
            )
        )
        for turn in (1, 2, 3):
            await handle.emit('turn_start', turn=turn, note=f'key={SECRET}')
        events = await asyncio.wait_for(reader, 5)
    epoch = created['as_of'].split(':')[0]
    assert [event['id'] for event in events] == [f'{epoch}:1', f'{epoch}:2', f'{epoch}:3']
    assert [event['data']['payload']['turn'] for event in events] == [1, 2, 3]
    assert all(SECRET not in json.dumps(event) for event in events)


async def test_a_reconnect_replays_what_it_missed_and_a_foreign_epoch_gets_a_reset_first():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        created = (await client.post('/api/v1/sessions', headers=server.login(), json={})).json()
        epoch = created['as_of'].split(':')[0]
        for turn in (1, 2, 3, 4):
            await assembly.handles[0].emit('turn_start', turn=turn)
        await asyncio.sleep(0.05)
        replayed = await sse_events(
            client, '/api/v1/sessions/s0/events', server.login(), 2, last_event_id=f'{epoch}:2'
        )
        foreign = await sse_events(
            client, '/api/v1/sessions/s0/events', server.login(), 1, last_event_id='other:2'
        )
    assert [event['id'] for event in replayed] == [f'{epoch}:3', f'{epoch}:4']
    assert foreign == [{'event': 'reset', 'data': {}}]


async def test_a_reconnect_older_than_the_ring_gets_a_reset():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        created = (await client.post('/api/v1/sessions', headers=server.login(), json={})).json()
        epoch = created['as_of'].split(':')[0]
        session = server.live.sessions['s0']
        for turn in range(2100):
            session.publish('turn_start', {'agent_id': 'a1', 'payload': {'turn': turn}})
        events = await sse_events(
            client, '/api/v1/sessions/s0/events', server.login(), 1, last_event_id=f'{epoch}:1'
        )
    assert events == [{'event': 'reset', 'data': {}}]


async def test_events_the_kernel_bus_dropped_turn_into_a_reset():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        handle = assembly.handles[0]
        session = server.live.sessions['s0']
        session._pump.cancel()  # pyright: ignore[reportPrivateUsage]
        follower = session.stream.follow(session.stream.position, heartbeat_s=60)
        seen: list[str] = []

        async def collect() -> None:
            async for item in follower:
                seen.append(item if isinstance(item, str) else item.kind)
                if len(seen) == 3:
                    return

        collecting = asyncio.create_task(collect())
        await asyncio.sleep(0)
        for turn in range(70):
            await handle.emit('turn_start', turn=turn)
        assert handle.subscription.dropped == 6
        for _ in range(2):
            session._observe(await anext(handle.subscription))  # pyright: ignore[reportPrivateUsage]
        await asyncio.wait_for(collecting, 2)
    # reset 不是流里的一条：没有序号，不进环形缓冲。丢过一次只发一次，之后的条目照常交出。
    kinds = [entry.kind for entry in session.stream._ring]  # pyright: ignore[reportPrivateUsage]
    assert kinds == ['turn_start', 'turn_start']
    assert seen == [RESET, 'turn_start', 'turn_start']


async def test_three_sessions_keep_their_events_to_themselves():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        for _ in range(3):
            await client.post('/api/v1/sessions', headers=server.login(), json={})
        for index, handle in enumerate(assembly.handles):
            await handle.emit('turn_start', agent_id=f'agent-{index}', turn=index)
        await asyncio.sleep(0.05)
        streams = [
            await sse_events(
                client,
                f'/api/v1/sessions/s{index}/events',
                server.login(),
                1,
                last_event_id=f'{server.live.sessions[f"s{index}"].stream.epoch}:0',
            )
            for index in range(3)
        ]
        global_events = await sse_events(
            client,
            '/api/v1/events',
            server.login(),
            6,
            last_event_id=f'{server.live.stream.epoch}:0',
        )
    for index, events in enumerate(streams):
        assert [event['data']['agent_id'] for event in events] == [f'agent-{index}']
    assert [(event['event'], event['data']['sid']) for event in global_events] == [
        ('session_opened', 's0'),
        ('session_opened', 's1'),
        ('session_opened', 's2'),
        ('turn_start', 's0'),
        ('turn_start', 's1'),
        ('turn_start', 's2'),
    ]


async def test_epochs_differ_between_sessions_and_between_runs():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        epochs = {server.live.sessions[sid].stream.epoch for sid in ('s0', 's1')}
        epochs.add(server.live.stream.epoch)
    assert len(epochs) == 3


async def _pending_confirmation(
    server: Any, assembly: FakeAssembly, client: httpx.AsyncClient, *, create: bool = True
):
    if create:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
    await assembly.handles[0].emit(
        'tool_confirmation_requested',
        agent_id='worker',
        request_id='r1',
        tool_call_id='c1',
        tool_name='Bash',
        arguments=json.dumps({'command': f'echo {SECRET}'}),
        verdict={'rule_id': 'x', 'level': 'high', 'reason': 'y'},
        untrusted=True,
    )
    await asyncio.sleep(0.05)


async def test_a_pending_confirmation_is_in_the_snapshot_and_answered_once():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _pending_confirmation(server, assembly, client)
        snapshot = (await client.get('/api/v1/sessions/s0/snapshot', headers=server.login())).json()
        socket = await _rpc(server.base, server.port, 's0', server.token)
        first = await _call(socket, op='answer', id=1, request_id='r1', text='approve')
        second = await _call(socket, op='answer', id=2, request_id='r1', text='approve')
        await socket.close()
    [pending] = snapshot['pending']
    assert (pending['kind'], pending['agent_id'], pending['untrusted']) == (
        'confirmation',
        'worker',
        True,
    )
    assert SECRET not in pending['arguments']
    assert first == {'id': 1, 'ok': True, 'error': None, 'result': None}
    assert second['ok'] is False and 'request_id' in second['error']['received']
    assert assembly.handles[0].answers == [('worker', 'r1', 'approve')]


@pytest.mark.parametrize('text', ['yes', 'APPROVE', ' approve', ''])
async def test_a_confirmation_answer_must_be_approve_or_reject(text: str):
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _pending_confirmation(server, assembly, client)
        socket = await _rpc(server.base, server.port, 's0', server.token)
        reply = await _call(socket, op='answer', id=1, request_id='r1', text=text)
        await socket.close()
        assert assembly.handles[0].answers == []
    assert reply['ok'] is False
    if text:
        assert 'approve' in reply['error']['expected']


async def test_a_question_answer_must_be_one_of_its_options_verbatim():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        await assembly.handles[0].emit(
            'user_question_requested', request_id='q1', question='Which?', options=['red', 'blue']
        )
        await asyncio.sleep(0.05)
        socket = await _rpc(server.base, server.port, 's0', server.token)
        wrong = await _call(socket, op='answer', id=1, request_id='q1', text='1')
        right = await _call(socket, op='answer', id=2, request_id='q1', text='blue')
        await socket.close()
    assert wrong['ok'] is False and "['red', 'blue']" in wrong['error']['expected']
    assert right['ok'] is True
    assert assembly.handles[0].answers == [('a1', 'q1', 'blue')]


async def test_a_resolved_request_leaves_the_pending_table():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _pending_confirmation(server, assembly, client)
        await assembly.handles[0].emit('human_request_resolved', request_id='r1', outcome='timeout')
        await asyncio.sleep(0.05)
        snapshot = (await client.get('/api/v1/sessions/s0/snapshot', headers=server.login())).json()
        socket = await _rpc(server.base, server.port, 's0', server.token)
        late = await _call(socket, op='answer', id=1, request_id='r1', text='approve')
        await socket.close()
    assert snapshot['pending'] == []
    assert late['ok'] is False
    assert assembly.handles[0].answers == []


async def test_after_dropped_events_only_requests_of_waiting_agents_stay_pending():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        handle = assembly.handles[0]
        session = server.live.sessions['s0']
        session._pump.cancel()  # pyright: ignore[reportPrivateUsage]
        snapshot = await handle.snapshot()
        snapshot['agents'] += [
            {'agent_id': 'worker', 'name': 'Worker', 'parent_id': 's0', 'status': 'waiting'},
            {'agent_id': 'gone', 'name': 'Worker', 'parent_id': 's0', 'status': 'idle'},
        ]

        async def waiting_state() -> dict[str, Any]:
            return snapshot

        handle.snapshot = waiting_state  # type: ignore[method-assign]
        for agent_id, request_id in (('worker', 'r1'), ('gone', 'r2')):
            request = {'request_id': request_id, 'tool_call_id': 'c', 'tool_name': 'Bash'}
            session._observe(Event('tool_confirmation_requested', agent_id, request))  # pyright: ignore[reportPrivateUsage]
        for turn in range(70):
            await handle.emit('turn_start', turn=turn)
        assert handle.subscription.dropped > 0
        session._observe(await anext(handle.subscription))  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
        assert sorted(session.pending) == ['r1']


async def test_unattended_sessions_reject_confirmations_and_abort_questions():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        await _pending_confirmation(server, assembly, client)
        handle = assembly.handles[0]
        await handle.emit('user_input_requested', agent_id='asker', request_id='u1', question='?')
        server.live.attach()
        server.live.detach()
        await asyncio.sleep(0.2)
        await handle.emit('user_input_requested', agent_id='late', request_id='u2', question='?')
        await asyncio.sleep(0.05)
    assert handle.answers == [('worker', 'r1', 'reject')]
    assert handle.aborted == ['asker', 'late']


async def test_an_unattended_decline_does_not_answer_a_request_resolved_before_it_ran():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        server.live.attach()
        server.live.detach()
        await asyncio.sleep(0.2)
        assert server.live.unattended is True
        [session] = server.live.sessions.values()
        payload = {'request_id': 'r9', 'tool_call_id': 'c9', 'tool_name': 'Bash'}
        # 同一拍里先到请求、再到它的了结：无人值守的拒绝已经排上，但还没跑。
        session._observe(Event('tool_confirmation_requested', 'worker', payload))  # pyright: ignore[reportPrivateUsage]
        session._observe(Event('human_request_resolved', 'worker', {'request_id': 'r9'}))  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
    assert assembly.handles[0].answers == []


async def test_a_connected_client_keeps_requests_waiting():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        server.live.attach()
        await _pending_confirmation(server, assembly, client)
        await asyncio.sleep(0.2)
        assert assembly.handles[0].answers == []
        server.live.detach()


async def test_a_session_nobody_ever_connected_to_is_unattended_after_the_grace():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        await _pending_confirmation(server, assembly, client)
        await asyncio.sleep(0.2)
    assert assembly.handles[0].answers == [('worker', 'r1', 'reject')]


async def test_an_open_event_stream_counts_as_an_attending_human():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base, timeout=10) as client,
    ):
        request = client.build_request('GET', '/api/v1/events', headers=server.login())
        stream = await client.send(request, stream=True)
        await asyncio.sleep(0.05)
        await _pending_confirmation(server, assembly, client)
        await asyncio.sleep(0.2)
        while_open = list(assembly.handles[0].answers)
        await stream.aclose()
        await asyncio.sleep(0.3)
    assert while_open == []
    assert assembly.handles[0].answers == [('worker', 'r1', 'reject')]


async def test_an_open_rpc_socket_counts_as_an_attending_human():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        socket = await _rpc(server.base, server.port, 's0', server.token)
        await _pending_confirmation(server, assembly, client, create=False)
        await asyncio.sleep(0.2)
        while_open = list(assembly.handles[0].answers)
        await socket.close()
        await asyncio.sleep(0.3)
    assert while_open == []
    assert assembly.handles[0].answers == [('worker', 'r1', 'reject')]


async def test_send_abort_and_grant_reach_the_handle():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        socket = await _rpc(server.base, server.port, 's0', server.token)
        sent = await _call(socket, op='send', id=1, text='hello', target='child')
        await _call(socket, op='send', id=2, text='again')
        aborted = await _call(socket, op='abort', id=3)
        granted = await _call(socket, op='grant', id=4, budget={'turns': 3})
        unknown = await _call(socket, op='explode', id=5)
        await socket.close()
    handle = assembly.handles[0]
    assert sent['result'] == {'message_id': 'm1'}
    assert handle.delivered == [('child', 'hello'), ('s0', 'again')]
    assert aborted['ok'] and handle.aborted == ['s0']
    assert granted['result'] == {'view': {'limit': {'turns': 3}}}
    assert unknown['ok'] is False and 'op' in unknown['error']['expected']


async def test_an_unexpected_error_in_an_op_is_a_reply_and_the_socket_stays_open():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        handle = assembly.handles[0]
        original = handle.deliver

        async def broken(target: str, text: str) -> Any:
            raise KeyError('store lost the row')

        handle.deliver = broken  # type: ignore[method-assign]
        socket = await _rpc(server.base, server.port, 's0', server.token)
        failed = await _call(socket, op='send', id=1, text='hello')
        handle.deliver = original  # type: ignore[method-assign]
        later = await _call(socket, op='send', id=2, text='again')
        await socket.close()
    assert failed['ok'] is False and failed['id'] == 1
    assert 'server 日志' in failed['error']['received']
    assert 'store lost the row' not in json.dumps(failed)
    assert later['ok'] is True


async def test_a_binary_frame_is_a_bad_frame_not_a_dropped_connection():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        socket = await _rpc(server.base, server.port, 's0', server.token)
        await socket.send(b'\x00\x01')
        refused = json.loads(await asyncio.wait_for(socket.recv(), 5))
        later = await _call(socket, op='send', id=2, text='again')
        await socket.close()
        unauthenticated = await websockets.connect(
            f'ws://127.0.0.1:{server.port}/api/v1/sessions/s0/rpc', origin=Origin(server.base)
        )
        await unauthenticated.send(b'\x00')
        first = json.loads(await asyncio.wait_for(unauthenticated.recv(), 5))
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(unauthenticated.recv(), 5)
    assert refused['ok'] is False and '二进制' in refused['error']['received']
    assert later['ok'] is True
    assert first['ok'] is False
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401


async def test_a_failed_delivery_is_reported_with_three_parts():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        assembly.handles[0].deliver_ok = False
        socket = await _rpc(server.base, server.port, 's0', server.token)
        reply = await _call(socket, op='send', id=1, text='hello')
        await socket.close()
    assert reply['ok'] is False
    assert reply['error']['received'] == 'agent is gone'


@pytest.mark.parametrize(
    'first', [None, {'op': 'auth', 'token': 'wrong'}, {'op': 'send', 'text': 'x'}]
)
async def test_a_websocket_that_does_not_authenticate_first_is_closed(first: dict[str, Any] | None):
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        socket = await websockets.connect(
            f'ws://127.0.0.1:{server.port}/api/v1/sessions/s0/rpc',
            origin=Origin(server.base),
        )
        if first is not None:
            await socket.send(json.dumps(first))
        refusal = json.loads(await asyncio.wait_for(socket.recv(), 8))
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(socket.recv(), 8)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401
    assert refusal['ok'] is False
    assert (
        'auth' in refusal['error']['expected'] and 'syndica up' in refusal['error']['next_action']
    )


async def test_a_first_frame_that_is_not_auth_does_not_authenticate_even_with_a_valid_token():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        socket = await websockets.connect(
            f'ws://127.0.0.1:{server.port}/api/v1/sessions/s0/rpc', origin=Origin(server.base)
        )
        await socket.send(json.dumps({'op': 'send', 'id': 1, 'token': server.token, 'text': 'x'}))
        refusal = json.loads(await asyncio.wait_for(socket.recv(), 8))
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(socket.recv(), 8)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401
    assert refusal['ok'] is False
    assert assembly.handles[0].delivered == []


async def test_shutdown_closes_every_session_at_once():
    assembly = FakeAssembly()
    release = asyncio.Event()
    started: list[str] = []
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        for _ in range(3):
            await client.post('/api/v1/sessions', headers=server.login(), json={})
        for handle in assembly.handles:
            original = handle.close

            async def slow(
                failure: BaseException | None, *, _h: FakeHandle = handle, _o: Any = original
            ):
                started.append(_h.sid)
                await release.wait()
                await _o(failure)

            handle.close = slow  # type: ignore[method-assign]
        closing = asyncio.create_task(server.live.shutdown())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(started) == 3:
                break
        concurrently = sorted(started)
        release.set()
        await asyncio.wait_for(closing, 5)
    assert concurrently == ['s0', 's1', 's2']


async def test_a_secret_in_an_event_is_redacted_on_both_event_streams():
    assembly = FakeAssembly()
    async with (
        running(assembly) as server,
        httpx.AsyncClient(base_url=server.base, timeout=10) as client,
    ):
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        session = server.live.sessions['s0']
        await assembly.handles[0].emit('turn_start', turn=1, note=f'key {SECRET}')
        await asyncio.sleep(0.05)
        global_events = await sse_events(
            client,
            '/api/v1/events',
            server.login(),
            1,
            last_event_id=f'{server.live.stream.epoch}:1',
        )
        session_events = await sse_events(
            client,
            '/api/v1/sessions/s0/events',
            server.login(),
            1,
            last_event_id=f'{session.stream.epoch}:0',
        )
    for events in (global_events, session_events):
        assert [event['event'] for event in events] == ['turn_start']
        assert SECRET not in json.dumps(events)
        assert '[REDACTED' in json.dumps(events)


async def test_unauthenticated_websockets_do_not_take_the_humans_stream_slots():
    assembly = FakeAssembly()
    async with (
        running(assembly) as server,
        httpx.AsyncClient(base_url=server.base, timeout=10) as client,
    ):
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        idle = [
            await websockets.connect(
                f'ws://127.0.0.1:{server.port}/api/v1/sessions/s0/rpc', origin=Origin(server.base)
            )
            for _ in range(MAX_STREAMS)
        ]
        stream = await client.send(
            client.build_request('GET', '/api/v1/events', headers=server.login()), stream=True
        )
        socket = await _rpc(server.base, server.port, 's0', server.token)
        status = stream.status_code
        await stream.aclose()
        await socket.close()
        for waiting in idle:
            await waiting.close()
    assert status == 200


async def test_authenticated_websockets_beyond_the_stream_limit_are_refused():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        held = [
            await _rpc(server.base, server.port, 's0', server.token) for _ in range(MAX_STREAMS)
        ]
        extra = await websockets.connect(
            f'ws://127.0.0.1:{server.port}/api/v1/sessions/s0/rpc', origin=Origin(server.base)
        )
        await extra.send(json.dumps({'op': 'auth', 'id': 0, 'token': server.token}))
        reply = json.loads(await asyncio.wait_for(extra.recv(), 2))
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(extra.recv(), 2)
        for socket in held:
            await socket.close()
    assert reply['ok'] is False
    assert str(MAX_STREAMS) in reply['error']['expected']
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 1013


async def test_the_oldest_unauthenticated_handshake_is_closed_when_too_many_wait():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        url = f'ws://127.0.0.1:{server.port}/api/v1/sessions/s0/rpc'
        first = await websockets.connect(url, origin=Origin(server.base))
        rest = [
            await websockets.connect(url, origin=Origin(server.base)) for _ in range(MAX_HANDSHAKES)
        ]
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(first.recv(), 2)
        socket = await _rpc(server.base, server.port, 's0', server.token)
        await socket.close()
        for waiting in rest:
            await waiting.close()
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4408


async def test_a_websocket_from_another_origin_is_closed():
    async with running(FakeAssembly()) as server:
        with pytest.raises(InvalidStatus):
            socket = await websockets.connect(
                f'ws://127.0.0.1:{server.port}/api/v1/sessions/s0/rpc',
                origin=Origin('http://evil.example'),
            )
            await socket.recv()


async def test_streams_beyond_the_limit_are_refused():
    assembly = FakeAssembly()
    async with (
        running(assembly) as server,
        httpx.AsyncClient(base_url=server.base, timeout=10) as client,
    ):
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        opened = []
        for _ in range(MAX_STREAMS):
            request = client.build_request('GET', '/api/v1/events', headers=server.login())
            opened.append(await client.send(request, stream=True))
        refused = await client.get('/api/v1/events', headers=server.login())
        for response in opened:
            await response.aclose()
    assert refused.status_code == 429
    assert str(MAX_STREAMS) in refused.json()['expected']


async def test_doctor_reports_each_failing_check_with_three_parts():
    assembly = FakeAssembly()
    assembly.checks = [
        {'name': 'config', 'ok': True},
        {
            'name': 'model default',
            'ok': False,
            'expected': 'GEMINI_API_KEY',
            'received': f'unset, saw {SECRET}',
            'next_action': 'export it',
        },
    ]
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        report = (await client.get('/api/v1/doctor', headers=server.login())).json()
    assert report['ok'] is False
    assert report['checks'][1]['received'] == 'unset, saw [REDACTED:secret.env_value]'


async def test_a_missing_frontend_is_a_three_part_error_not_a_blank_page():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get('/')
    assert response.status_code == 503
    assert 'syndica[server]' in response.json()['next_action']


async def test_the_frontend_is_served_and_paths_cannot_leave_its_directory(tmp_path: Path):
    static = tmp_path / 'static'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<!doctype html><title>Syndica</title>')
    (static / 'assets' / 'app.js').write_text('console.log(1)')
    (tmp_path / 'secret.txt').write_text('outside')
    async with (
        running(FakeAssembly(), static_dir=static) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        index = await client.get('/sessions/abc')
        asset = await client.get('/assets/app.js')
        escape = await client.get('/assets/..%2F..%2Fsecret.txt')
        api = await client.get('/api/v1/nope', headers=server.login())
    assert 'Syndica' in index.text
    assert asset.text == 'console.log(1)'
    assert 'outside' not in escape.text
    assert api.status_code == 404


async def test_closing_a_session_rejects_its_pending_confirmation_and_closes_the_handle():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _pending_confirmation(server, assembly, client)
        closed = await client.post('/api/v1/sessions/s0/close', headers=server.login())
        missing = await client.get('/api/v1/sessions/s0/snapshot', headers=server.login())
    handle = assembly.handles[0]
    assert closed.status_code == 204
    assert handle.answers == [('worker', 'r1', 'reject')]
    assert handle.closed == [None]
    assert missing.status_code == 404 and 'resume' in missing.json()['next_action']


async def test_server_shutdown_closes_every_live_session():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        for _ in range(3):
            await client.post('/api/v1/sessions', headers=server.login(), json={})
    assert [handle.closed for handle in assembly.handles] == [[None], [None], [None]]


async def test_resume_reuses_a_live_session_and_rm_removes_the_stored_one():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        first = await client.post('/api/v1/sessions/stored/resume', headers=server.login())
        again = await client.post('/api/v1/sessions/stored/resume', headers=server.login())
        removed = await client.delete('/api/v1/sessions/stored', headers=server.login())
    assert first.status_code == 200 and again.status_code == 200
    assert [options['resume'] for options in assembly.opened] == ['stored']
    assert removed.status_code == 204
    assert assembly.removed == ['stored']
    assert assembly.handles[0].closed == [None]


async def test_resume_takes_a_budget_and_a_wall_clock_starts_fresh():
    assembly = FakeAssembly()
    budget = {'budget': {'wall_clock_s': 600.0, 'turns': 5}}
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        resumed = await client.post(
            '/api/v1/sessions/stored/resume', headers=server.login(), json=budget
        )
    assert resumed.status_code == 200
    [options] = assembly.opened
    assert (options['wall_clock_s'], options['turns'], options['tokens']) == (600.0, 5, None)


async def test_a_budget_for_a_session_that_is_already_open_is_refused_not_dropped():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions/stored/resume', headers=server.login())
        refused = await client.post(
            '/api/v1/sessions/stored/resume',
            headers=server.login(),
            json={'budget': {'wall_clock_s': 600.0}},
        )
    assert refused.status_code == 409
    assert 'grant' in refused.json()['next_action']
    assert len(assembly.opened) == 1


async def test_an_error_that_echoes_the_callers_input_is_redacted():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        host = await client.get('/api/v1/health', headers={'host': f'{SECRET}.example:1'})
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        socket = await _rpc(server.base, server.port, 's0', server.token)
        reply = await _call(socket, op='answer', id=1, request_id=SECRET, text='approve')
        await socket.close()
    assert host.status_code == 421 and SECRET not in host.text
    assert reply['ok'] is False and SECRET not in json.dumps(reply)


async def test_reconnecting_ends_unattended_mode():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        server.live.attach()
        server.live.detach()
        await asyncio.sleep(0.2)
        assert server.live.unattended is True
        server.live.attach()
        await assembly.handles[0].emit('user_input_requested', request_id='u1', question='?')
        await asyncio.sleep(0.1)
        assert assembly.handles[0].aborted == []
        server.live.detach()


class _BusyHandle(FakeHandle):
    """快照期间又有一条事件进了流：位置必须在它之前，客户端才会拿到它。"""

    live: Any = None

    async def snapshot(self) -> dict[str, Any]:
        self.live.sessions[self.sid].publish('turn_start', {'agent_id': 'a1', 'payload': {}})
        return await super().snapshot()


class _BusyAssembly(FakeAssembly):
    async def open(self, **options: Any) -> FakeHandle:
        handle = _BusyHandle(f's{len(self.handles)}')
        self.handles.append(handle)
        return handle


async def test_the_snapshot_position_is_taken_before_the_snapshot():
    assembly = _BusyAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        _BusyHandle.live = server.live
        created = (await client.post('/api/v1/sessions', headers=server.login(), json={})).json()
        events = await sse_events(
            client, '/api/v1/sessions/s0/events', server.login(), 1, last_event_id=created['as_of']
        )
    assert created['as_of'].endswith(':0')
    assert events[0]['event'] == 'turn_start'


async def test_a_budget_question_waits_for_a_budget_answer_and_is_withdrawn_after():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        ask = assembly.opened[0]['ask_budget']
        asking = asyncio.create_task(ask('worker', {'remaining': {'turns': 0}}))
        await asyncio.sleep(0.05)
        snapshot = (await client.get('/api/v1/sessions/s0/snapshot', headers=server.login())).json()
        [pending] = snapshot['pending']
        socket = await _rpc(server.base, server.port, 's0', server.token)
        wrong = await _call(socket, op='answer', id=1, request_id=pending['request_id'], text='x')
        reply = await _call(
            socket, op='budget_answer', id=2, request_id=pending['request_id'], budget={'turns': 4}
        )
        granted = await asyncio.wait_for(asking, 5)
        again = await _call(
            socket, op='budget_answer', id=3, request_id=pending['request_id'], budget=None
        )
        await socket.close()
        after = (await client.get('/api/v1/sessions/s0/snapshot', headers=server.login())).json()
        kinds = [entry.kind for entry in server.live.sessions['s0'].stream._ring]  # pyright: ignore[reportPrivateUsage]
    assert (pending['kind'], pending['agent_id']) == ('budget', 'worker')
    assert wrong['ok'] is False and 'budget_answer' in wrong['error']['next_action']
    assert reply['ok'] is True
    assert granted is not None and granted.turns == 4
    assert again['ok'] is False
    assert after['pending'] == []
    assert kinds == ['budget_question', 'budget_question_resolved']


async def test_an_unattended_budget_question_gets_no_extra_budget_at_once():
    assembly = FakeAssembly()
    async with (
        running(assembly, grace_s=0.05) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        await client.post('/api/v1/sessions', headers=server.login(), json={})
        server.live.attach()
        server.live.detach()
        await asyncio.sleep(0.2)
        answer = await asyncio.wait_for(assembly.opened[0]['ask_budget']('worker', {}), 1)
    assert answer is None
