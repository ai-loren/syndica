"""阶段二、三的读模型与终端：产物、Rubric、trace、flags、拓扑、两种终端、进度缓存。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import websockets
from server_fakes import ARTIFACT, CONTENT, SECRET, FakeAssembly, running
from websockets.typing import Origin


async def _socket(base: str, port: int, path: str, token: str, **auth: Any) -> Any:
    socket = await websockets.connect(f'ws://127.0.0.1:{port}{path}', origin=Origin(base))
    await socket.send(json.dumps({'op': 'auth', 'id': 0, 'token': token, **auth}))
    return socket


async def _session(client: httpx.AsyncClient, server: Any) -> None:
    await client.post('/api/v1/sessions', headers=server.login(), json={})


async def test_artifact_listing_versions_preview_and_diff_are_redacted_json():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        base = '/api/v1/sessions/s0/artifacts'
        listing = await client.get(base, headers=server.login())
        versions = await client.get(
            f'{base}/versions', params={'id': ARTIFACT['id']}, headers=server.login()
        )
        preview = await client.get(
            f'{base}/preview',
            params={'id': ARTIFACT['id'], 'version': 'sha256:old'},
            headers=server.login(),
        )
        diff = await client.get(
            f'{base}/diff',
            params={'id': ARTIFACT['id'], 'old': 'sha256:old', 'new': 'sha256:new'},
            headers=server.login(),
        )
        missing = await client.get(
            f'{base}/diff', params={'id': ARTIFACT['id']}, headers=server.login()
        )
    assert listing.json() == {'artifacts': [{**ARTIFACT, 'versions': 2, 'note': ''}]}
    assert [row['version'] for row in versions.json()['versions']] == ['sha256:old', 'sha256:new']
    assert preview.headers['content-type'] == 'application/json'
    assert preview.json()['text'].startswith('<script>') and SECRET not in preview.text
    assert diff.json()['changed'] is True and SECRET not in diff.text
    assert assembly.handles[0].calls[-1] == ('diff', ARTIFACT['id'], 'sha256:old', 'sha256:new')
    assert missing.status_code == 422 and 'old' in missing.json()['expected']


async def test_the_latest_delivery_note_is_listed_with_the_artifact_and_redacted():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        handle = assembly.handles[0]
        await handle.emit('artifact_registered', ref=dict(ARTIFACT), note='first draft')
        await handle.emit('artifact_registered', ref=dict(ARTIFACT), note=f'final, key {SECRET}')
        await handle.emit('artifact_registered', ref=dict(ARTIFACT), note='')
        await asyncio.sleep(0.05)
        listing = await client.get('/api/v1/sessions/s0/artifacts', headers=server.login())
    [row] = listing.json()['artifacts']
    assert row['note'].startswith('final, key ') and SECRET not in listing.text


async def test_artifact_content_is_a_sandboxed_attachment_that_never_executes():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        response = await client.get(
            '/api/v1/sessions/s0/artifacts/content',
            params={'id': ARTIFACT['id'], 'version': 'sha256:old'},
            headers=server.login(),
        )
        without_token = await client.get(
            '/api/v1/sessions/s0/artifacts/content',
            params={'id': ARTIFACT['id'], 'version': 'sha256:old'},
        )
    assert response.content == CONTENT
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['content-security-policy'] == "sandbox; default-src 'none'"
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-syndica-mime'] == 'text/html'
    assert without_token.status_code == 401


async def test_feedback_with_a_line_range_becomes_a_proposed_rubric():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        created = await client.post(
            '/api/v1/sessions/s0/artifacts/feedback',
            headers=server.login(),
            json={
                'id': ARTIFACT['id'],
                'version': 'sha256:old',
                'text': '结论太弱',
                'start_line': 2,
                'end_line': 4,
            },
        )
        single = await client.post(
            '/api/v1/sessions/s0/artifacts/feedback',
            headers=server.login(),
            json={'id': ARTIFACT['id'], 'version': 'sha256:old', 'text': '这里', 'start_line': 3},
        )
        backwards = await client.post(
            '/api/v1/sessions/s0/artifacts/feedback',
            headers=server.login(),
            json={
                'id': ARTIFACT['id'],
                'version': 'sha256:old',
                'text': 'x',
                'start_line': 4,
                'end_line': 2,
            },
        )
        empty = await client.post(
            '/api/v1/sessions/s0/artifacts/feedback',
            headers=server.login(),
            json={'id': ARTIFACT['id'], 'version': 'sha256:old', 'text': ''},
        )
    assert created.status_code == 201
    assert created.json()['rubric']['status'] == 'proposed'
    feedback = [call for call in assembly.handles[0].calls if call[0] == 'feedback']
    assert [call[4] for call in feedback] == [(2, 4), (3, 3)]
    assert single.status_code == 201
    assert backwards.status_code == 422 and 'end_line' in backwards.json()['expected']
    assert empty.status_code == 422


async def test_rubrics_can_be_listed_and_activated_through_the_rpc():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        listing = await client.get('/api/v1/sessions/s0/rubrics', headers=server.login())
        socket = await _socket(server.base, server.port, '/api/v1/sessions/s0/rpc', server.token)
        await socket.recv()
        await socket.send(json.dumps({'op': 'activate_rubric', 'id': 1, 'rubric_id': 'feedback-1'}))
        reply = json.loads(await socket.recv())
        await socket.close()
    assert listing.json() == {'goal': 'g', 'rubrics': []}
    assert reply['ok'] is True
    assert ('activate', 'feedback-1') in assembly.handles[0].calls


async def test_traces_topologies_and_flags_are_served():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        traces = await client.get('/api/v1/traces', headers=server.login())
        trace = await client.get('/api/v1/traces/trace-1', headers=server.login())
        missing = await client.get('/api/v1/traces/nope', headers=server.login())
        own = await client.get('/api/v1/sessions/s0/trace', headers=server.login())
        assembly.handles[0].trace = None
        unrecorded = await client.get('/api/v1/sessions/s0/trace', headers=server.login())
        topologies = await client.get('/api/v1/topologies', headers=server.login())
        flags = await client.get('/api/v1/sessions/s0/flags', headers=server.login())
    assert [row['trace_id'] for row in traces.json()['traces']] == ['trace-1']
    assert [call['call_id'] for call in trace.json()['tool_calls']] == ['c1']
    assert missing.status_code == 400
    assert own.json() == trace.json()
    assert unrecorded.status_code == 404 and 'trace.level' in unrecorded.json()['next_action']
    assert topologies.json()['topologies'][0]['name'] == 'team'
    assert SECRET not in flags.text and flags.json()['flags'][0]['key'] == 'x'


async def test_the_human_terminal_relays_input_resize_and_output_and_closes_with_the_socket():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        socket = await _socket(
            server.base, server.port, '/api/v1/sessions/s0/pty', server.token, cols=80, rows=24
        )
        await socket.send(json.dumps({'op': 'input', 'data': 'ls\r'}))
        output = await asyncio.wait_for(socket.recv(), 5)
        await socket.send(json.dumps({'op': 'resize', 'cols': 120, 'rows': 40}))
        await socket.send('not json')
        await socket.send(json.dumps({'op': 'input', 'data': 'pwd\r'}))
        await asyncio.wait_for(socket.recv(), 5)
        await socket.close()
        await asyncio.sleep(0.1)
        [terminal] = assembly.handles[0].terminals
    assert output == b'echo:ls\r'
    assert terminal.written == [b'ls\r', b'pwd\r']
    assert terminal.size == (120, 40)
    assert terminal.closed is True


async def test_a_terminal_socket_without_authentication_opens_no_terminal():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        socket = await _socket(server.base, server.port, '/api/v1/sessions/s0/pty', 'wrong')
        refusal = json.loads(await asyncio.wait_for(socket.recv(), 8))
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(socket.recv(), 8)
    assert refusal['ok'] is False
    assert assembly.handles[0].terminals == []


async def test_the_agent_terminal_mirror_sends_whole_redacted_lines_and_ends_with_the_command():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        half = len(SECRET) // 2
        assembly.handles[0].screens['pty-1'] = [
            f'token={SECRET[:half]}',
            f'{SECRET[half:]}\nnext ',
            'line\n',
        ]
        socket = await _socket(
            server.base, server.port, '/api/v1/sessions/s0/agent-pty/pty-1', server.token
        )
        frames = []
        while True:
            try:
                frames.append(json.loads(await asyncio.wait_for(socket.recv(), 5)))
            except websockets.exceptions.ConnectionClosed:
                break
    text = ''.join(frame['text'] for frame in frames)
    assert SECRET[:half] not in text and SECRET[half:] not in text
    assert text.endswith('next line\n')
    assert frames[-1]['running'] is False


async def test_mirroring_a_terminal_that_does_not_exist_says_so():
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        socket = await _socket(
            server.base, server.port, '/api/v1/sessions/s0/agent-pty/missing', server.token
        )
        frame = json.loads(await asyncio.wait_for(socket.recv(), 5))
    assert frame['error']['next_action'] == 'list them'


async def test_the_latest_progress_of_each_agent_is_in_the_snapshot():
    assembly = FakeAssembly()
    async with running(assembly) as server, httpx.AsyncClient(base_url=server.base) as client:
        await _session(client, server)
        handle = assembly.handles[0]
        for satisfied in (['R1'], ['R1', 'R2']):
            await handle.emit(
                'progress_measured',
                agent_id='worker',
                satisfied=satisfied,
                mandatory=['R1', 'R2'],
                pending_human=[],
                measured_turn=len(satisfied),
                verdict='partial',
            )
        await asyncio.sleep(0.05)
        snapshot = (await client.get('/api/v1/sessions/s0/snapshot', headers=server.login())).json()
    assert snapshot['progress'] == {
        'worker': {
            'satisfied': ['R1', 'R2'],
            'mandatory': ['R1', 'R2'],
            'pending_human': [],
            'measured_turn': 2,
            'verdict': 'partial',
        }
    }
