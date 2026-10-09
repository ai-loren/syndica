"""中立轨迹：键序、unverified 默认排除、占位符保留、偏好对来自同一目标、工具调用随消息导出。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syndica.kernel import ConfigError, RuleRedactor
from syndica.runner.training import (
    SCHEMA,
    Attempt,
    Call,
    Message,
    export_preference,
    export_sft,
    rl_row,
    trajectory,
    write_export,
)

_GOLDEN = Path(__file__).parent / 'testdata' / 'trajectory.json'
_PLACEHOLDER = '[REDACTED:secret.token]'
_SECRET = 'sk-train-export-secret'


def _attempt(verdict: str, *, ident: str = 'try-1', content: str = 'wrote the note') -> Attempt:
    return Attempt(
        id=ident,
        goal_id='goal-planets',
        rubric_id='rubric-1',
        messages=(
            Message('user', 'write planets.md'),
            Message(
                'assistant',
                content,
                tool_calls=(Call('call-1', 'Write', '{"path": "planets.md"}'),),
            ),
            Message('tool', _PLACEHOLDER, tool_call_id='call-1'),
        ),
        artifacts=({'path': 'planets.md', 'version': '1'},),
        verdict=verdict,
    )


def test_a_trajectory_matches_the_golden_key_order() -> None:
    document = trajectory(_attempt('pass'))
    assert list(document) == [
        'schema',
        'id',
        'goal_id',
        'rubric_id',
        'messages',
        'tool_calls',
        'artifacts',
        'verdict',
        'causality',
        'timing',
        'redaction',
    ]
    timing = document['timing']
    assert isinstance(timing, dict)
    assert list(timing) == ['start_ns', 'end_ns', 'cost']
    redaction = document['redaction']
    assert isinstance(redaction, dict)
    assert list(redaction) == ['placeholders_kept', 'raw_secrets_exported']
    messages = document['messages']
    assert isinstance(messages, list)
    assert list(messages[0]) == ['role', 'content', 'tool_call_id', 'tool_calls']
    assert list(messages[1]['tool_calls'][0]) == ['id', 'name', 'arguments']
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
    assert rendered == _GOLDEN.read_text(encoding='utf-8')
    assert document['schema'] == SCHEMA
    assert _PLACEHOLDER in rendered


def test_unverified_is_left_out_unless_asked_and_a_secret_is_never_written() -> None:
    clean = _attempt('pass', ident='ok')
    unverified = _attempt('unverified', ident='skip-me')
    leaked = _attempt('partial', ident='leaked', content=f'note {_SECRET}')
    original = [clean, unverified, leaked]
    messages, _share, manifest = export_sft(original, secrets=(_SECRET,))
    assert 'skip-me' not in messages
    assert _SECRET not in messages
    assert manifest['dropped_unverified'] == ['skip-me']
    assert manifest['dropped_secret'] == ['leaked']
    assert manifest['include_unverified'] is False
    assert manifest['sources'] == ['ok']
    again, _, included = export_sft([unverified], include_unverified=True, secrets=())
    assert _PLACEHOLDER in again
    assert included['sources'] == ['skip-me']
    assert included['include_unverified'] is True
    assert original[0] is clean


def test_sft_rows_carry_tool_calls_tool_results_and_artifacts_in_order() -> None:
    messages, share, _ = export_sft([_attempt('pass')])
    [row] = [json.loads(line) for line in messages.splitlines()]
    assert list(row) == ['id', 'messages', 'artifacts']
    assert row['id'] == 'try-1'
    assert [item['role'] for item in row['messages']] == ['user', 'assistant', 'tool']
    assert row['messages'][1]['tool_calls'] == [
        {
            'id': 'call-1',
            'type': 'function',
            'function': {'name': 'Write', 'arguments': '{"path": "planets.md"}'},
        }
    ]
    assert row['messages'][2]['tool_call_id'] == 'call-1'
    assert row['artifacts'] == [{'path': 'planets.md', 'version': '1'}]
    [shared] = [json.loads(line) for line in share.splitlines()]
    assert [item['from'] for item in shared['conversations']] == [
        'human',
        'gpt',
        'function_call',
        'observation',
    ]


def test_include_unverified_rejects_a_yaml_bool_lookalike() -> None:
    with pytest.raises(ConfigError, match='include_unverified'):
        export_sft([_attempt('pass')], include_unverified=1)  # type: ignore[arg-type]


def test_one_bad_attempt_does_not_drop_the_rest() -> None:
    bad = Attempt('bad', 'goal-planets', 'rubric-1', (Message('nope', 'x'),), 'pass')
    messages, _, manifest = export_sft([bad, _attempt('pass', ident='ok')])
    assert '"ok"' in messages
    assert manifest['failed']
    assert 'bad' in str(manifest['failed'])


def test_a_tool_call_on_a_user_message_is_refused() -> None:
    bad = Attempt(
        'bad',
        'g',
        'r',
        (Message('user', 'x', tool_calls=(Call('c', 'Write'),)),),
        'pass',
    )
    with pytest.raises(ConfigError, match='assistant'):
        trajectory(bad)


def test_preference_pairs_come_from_the_same_goal_and_rubric() -> None:
    other = Attempt('other', 'goal-other', 'rubric-1', (Message('user', 'elsewhere'),), 'block')
    body, manifest = export_preference(
        [
            _attempt('pass', ident='chosen'),
            _attempt('block', ident='rejected'),
            other,
            _attempt('unverified', ident='hidden'),
        ]
    )
    lines = [json.loads(line) for line in body.splitlines()]
    assert lines == [
        {
            'goal_id': 'goal-planets',
            'rubric_id': 'rubric-1',
            'chosen': {'id': 'chosen', 'verdict': 'pass'},
            'rejected': {'id': 'rejected', 'verdict': 'block'},
        }
    ]
    assert manifest['preference_pairs'] == 1
    assert 'hidden' not in body


def test_rl_row_masks_only_assistant_tokens_including_its_tool_calls() -> None:
    row = rl_row(_attempt('partial'), lambda text: [len(text), 7])
    assert row['reward'] == 0.5
    mask = row['loss_mask']
    ids = row['input_ids']
    assert isinstance(mask, list) and isinstance(ids, list)
    assert len(mask) == len(ids)
    # 每段两个 token：user 0、assistant 正文 1、assistant 的工具调用 1、tool 0。
    assert mask == [0, 0, 1, 1, 1, 1, 0, 0]
    episode = row['episode']
    assert isinstance(episode, dict)
    assert episode['verdict'] == 'partial'
    assert episode['goal_id'] == 'goal-planets'


def test_export_does_not_rewrite_the_attempt() -> None:
    attempt = _attempt('pass')
    before = attempt.messages[1].content
    export_sft([attempt])
    assert attempt.messages[1].content == before
    assert attempt.verdict == 'pass'


_QUOTED_SECRET = 'plant"ed-quiz-secret-value'
_REDACTED_NOTE = 'note [REDACTED:secret.env_value]'


@pytest.mark.parametrize(
    ('fmt', 'name', 'content_of'),
    [
        ('sft', 'messages.jsonl', lambda row: row['messages'][1]['content']),
        ('sharegpt', 'sharegpt.jsonl', lambda row: row['conversations'][1]['value']),
    ],
    ids=['sft', 'sharegpt'],
)
def test_write_export_redacts_each_leaf_before_encoding(
    tmp_path: Path, fmt: str, name: str, content_of: object
) -> None:
    attempt = _attempt('pass', content=f'note {_QUOTED_SECRET}')
    redactor = RuleRedactor(secrets=[_QUOTED_SECRET])
    manifest = write_export([attempt], fmt, tmp_path / 'out', redactor=redactor)
    assert manifest['format'] == fmt
    [row] = [json.loads(line) for line in (tmp_path / 'out' / name).read_text().splitlines()]
    assert callable(content_of)
    # 整段编码之后再脱敏，转义过的引号会让规则只换掉密钥的后半截，留下 `plant\`。
    assert content_of(row) == _REDACTED_NOTE
    assert attempt.messages[1].content == f'note {_QUOTED_SECRET}'


def test_write_export_rl_tokenizes_the_redacted_text(tmp_path: Path) -> None:
    attempt = _attempt('pass', content=f'note {_QUOTED_SECRET}')
    redactor = RuleRedactor(secrets=[_QUOTED_SECRET])
    write_export(
        [attempt], 'rl', tmp_path / 'out', redactor=redactor, tokenize=lambda text: [len(text)]
    )
    [row] = [json.loads(line) for line in (tmp_path / 'out' / 'rl.jsonl').read_text().splitlines()]
    assert row['input_ids'][1] == len(_REDACTED_NOTE)


def test_write_export_rl_without_a_tokenizer_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match='tokenize'):
        write_export([_attempt('pass')], 'rl', tmp_path / 'out')
    assert not (tmp_path / 'out').exists()
