"""OpenAPI 快照：发布即承诺。快照一变，api-changes.md 就要有一条带新摘要的记录。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from server_fakes import FakeAssembly
from starlette.routing import Route, WebSocketRoute

from syndica.kernel.events import KERNEL_EVENTS
from syndica.server import Credentials, Live, ServerSettings, create_app
from syndica.server.openapi import PATHS, SERVER_EVENTS, openapi

_REFERENCE = Path(__file__).resolve().parents[2] / 'docs' / 'reference'
_SNAPSHOT = _REFERENCE / 'openapi.json'
_CHANGES = _REFERENCE / 'api-changes.md'


def _rendered() -> str:
    return json.dumps(openapi(), ensure_ascii=False, indent=2) + '\n'


def test_the_openapi_snapshot_matches_the_models():
    if os.environ.get('SYNDICA_UPDATE_OPENAPI') == '1':
        _SNAPSHOT.write_text(_rendered(), encoding='utf-8')
    assert _SNAPSHOT.read_text(encoding='utf-8') == _rendered(), (
        'HTTP 契约变了：SYNDICA_UPDATE_OPENAPI=1 重新生成快照，看 diff，'
        '再在 docs/reference/api-changes.md 记一条带新摘要的记录'
    )


def test_the_current_snapshot_is_recorded_in_the_change_log():
    digest = hashlib.sha256(_SNAPSHOT.read_bytes()).hexdigest()[:12]
    assert f'`{digest}`' in _CHANGES.read_text(encoding='utf-8'), (
        f'快照摘要 {digest} 不在 api-changes.md 里：写明这次改了什么、是不是破坏性变更'
    )


def test_the_documented_paths_are_exactly_the_routes_the_app_serves():
    guard: Any = create_app(
        Live(FakeAssembly()),
        Credentials(),
        ServerSettings(port=1, nonce='n', version='v'),
    )
    routes = guard._app.routes
    served = {
        (method.lower(), route.path)
        for route in routes  # pyright: ignore[reportUnknownVariableType]
        if isinstance(route, Route) and route.path != '/{path:path}'
        for method in (route.methods or set()) - {'HEAD'}
    }
    sockets = sorted(route.path for route in routes if isinstance(route, WebSocketRoute))  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    assert served == {(method, path) for method, path, *_ in PATHS}
    assert sockets == sorted(
        [
            openapi()['x-syndica-rpc']['path'],
            '/api/v1/sessions/{sid}/pty',
            '/api/v1/sessions/{sid}/agent-pty/{agent_id}/{handle}',
        ]
    )
    assert all(path in json.dumps(openapi()['x-syndica-pty']) for path in sockets[:2])


def test_protocol_routes_are_referenced_not_expanded():
    spec = openapi()
    card = spec['paths']['/.well-known/agent-card.json']['get']
    chat = spec['paths']['/v1/chat/completions']['post']
    assert card['x-syndica-protocol'] == 'a2a'
    assert chat['x-syndica-protocol'] == 'openai'
    assert card['responses']['200']['content']['application/json']['schema'] == {
        '$ref': '#/components/schemas/ExternalProtocol'
    }
    assert 'requestBody' not in card
    assert chat['requestBody']['content']['application/json']['schema']['$ref'].endswith(
        'ExternalProtocol'
    )
    schema = spec['components']['schemas']['ExternalProtocol']
    assert schema['additionalProperties'] is True
    assert 'properties' not in schema


def test_every_event_kind_a_client_can_receive_has_a_schema():
    events = openapi()['x-syndica-events']
    assert set(events) == set(KERNEL_EVENTS) | set(SERVER_EVENTS)
    assert events['message_appended']['required'] == ['generation', 'index', 'block']
    assert events['budget_question']['required'] == ['request_id', 'view']


def test_no_field_is_marked_provisional():
    assert 'x-syndica-stability' not in json.dumps(openapi())


def test_the_snapshot_budget_and_progress_have_typed_schemas():
    schemas = openapi()['components']['schemas']
    properties = schemas['Snapshot']['properties']
    assert properties['budget']['anyOf'][0] == {'$ref': '#/components/schemas/BudgetViewOut'}
    assert properties['progress']['anyOf'][0]['additionalProperties'] == {
        '$ref': '#/components/schemas/ProgressOut'
    }
    assert list(schemas['BudgetViewOut']['properties']) == [
        'limit',
        'used',
        'remaining',
        'exhausted',
    ]
    assert schemas['ProgressOut']['required'] == [
        'satisfied',
        'mandatory',
        'pending_human',
        'measured_turn',
        'verdict',
    ]


def test_the_typed_budget_and_progress_accept_what_the_kernel_produces():
    from decimal import Decimal

    from syndica.kernel import Budget, BudgetView, Usage
    from syndica.kernel.transcript import budget_payload
    from syndica.server.models import BudgetViewOut, ProgressOut

    view = BudgetView(
        limit=Budget(turns=10, cost=Decimal('0.50'), wall_clock_s=30.0),
        used=Usage(input_tokens=3, output_tokens=4, turns=2, estimated=True),
        remaining=Budget(turns=8, cost=None),
        exhausted=('tokens',),
    )
    payload = budget_payload(view)
    assert BudgetViewOut.model_validate(payload).model_dump() == payload
    progress = {
        'satisfied': ['R1'],
        'mandatory': ['R1', 'R2'],
        'pending_human': [],
        'measured_turn': 7,
        'verdict': 'partial',
    }
    assert ProgressOut.model_validate(progress).model_dump() == progress
