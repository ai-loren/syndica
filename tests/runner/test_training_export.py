"""中立轨迹：键序、unverified 默认排除、占位符保留、偏好对来自同一目标、工具调用随消息导出、
写盘纪律。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from syndica.kernel import ConfigError, RuleRedactor
from syndica.kernel.redaction import RULES_VERSION
from syndica.runner.training import (
    FORMATS,
    SCHEMA,
    Attempt,
    Call,
    Message,
    export_preference,
    export_sft,
    read_trajectory,
    rl_row,
    trajectory,
    write_export,
)

_GOLDEN = Path(__file__).parent / 'testdata' / 'trajectory.json'
_PLACEHOLDER = '[REDACTED:secret.token]'
_SECRET = 'sk-train-export-secret'
_QUOTED_SECRET = 'plant"ed-quiz-secret-value'
_REDACTED_NOTE = 'note [REDACTED:secret.env_value]'


def _attempt(
    verdict: str,
    *,
    ident: str = 'try-1',
    content: str = 'wrote the note',
    arguments: str = '{"path": "planets.md"}',
    trust: str = 'trusted',
) -> Attempt:
    return Attempt(
        id=ident,
        goal_id='goal-planets',
        rubric_id='rubric-1',
        messages=(
            Message('user', 'write planets.md'),
            Message('assistant', content, tool_calls=(Call('call-1', 'Write', arguments),)),
            Message('tool', _PLACEHOLDER, tool_call_id='call-1', trust=trust),
        ),
        artifacts=({'path': 'planets.md', 'version': '1'},),
        verdict=verdict,
        model='scripted',
        system_prompt='you write notes',
        tools=('Read', 'Write'),
        outcomes=('R1=satisfied',),
        trace_id='t' * 32,
    )


def test_a_trajectory_matches_the_golden_key_order() -> None:
    document = trajectory(_attempt('pass'), redaction={'digest': 'd' * 64, 'rules_version': 2})
    assert list(document) == [
        'schema',
        'id',
        'goal_id',
        'rubric_id',
        'model',
        'system_prompt',
        'tools',
        'messages',
        'tool_calls',
        'artifacts',
        'verdict',
        'verdicts',
        'outcomes',
        'source',
        'causality',
        'timing',
        'redaction',
    ]
    source = document['source']
    assert isinstance(source, dict) and list(source) == ['trace_id', 'verdict_source']
    timing = document['timing']
    assert isinstance(timing, dict) and list(timing) == ['start_ns', 'end_ns', 'cost']
    redaction = document['redaction']
    assert isinstance(redaction, dict)
    assert list(redaction) == [
        'digest',
        'rules_version',
        'placeholders_kept',
        'raw_secrets_exported',
    ]
    messages = document['messages']
    assert isinstance(messages, list)
    assert list(messages[0]) == ['role', 'content', 'tool_call_id', 'tool_calls', 'trust']
    assert list(messages[1]['tool_calls'][0]) == ['id', 'name', 'arguments']
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
    assert rendered == _GOLDEN.read_text(encoding='utf-8')
    assert document['schema'] == SCHEMA
    assert _PLACEHOLDER in rendered


def test_read_trajectory_accepts_its_own_schema_and_refuses_another() -> None:
    document = trajectory(_attempt('pass'))
    assert read_trajectory(json.loads(json.dumps(document))) == document
    with pytest.raises(ConfigError, match='syndica.trajectory/1'):
        read_trajectory({**document, 'schema': 'syndica.trajectory/2'})
    partial = {key: value for key, value in document.items() if key != 'verdicts'}
    with pytest.raises(ConfigError, match='verdicts'):
        read_trajectory(partial)


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


@pytest.mark.parametrize(
    'secret',
    ['quote"inside-quiz-value', 'back\\slash-quiz-value', 'line\nbreak-quiz-value'],
    ids=['quote', 'backslash', 'newline'],
)
@pytest.mark.parametrize('where', ['content', 'arguments'])
def test_a_named_secret_that_json_escapes_is_still_dropped(secret: str, where: str) -> None:
    attempt = (
        _attempt('pass', ident='escaped', content=f'note {secret}')
        if where == 'content'
        else _attempt('pass', ident='escaped', arguments=json.dumps({'token': secret}))
    )
    _, _, manifest = export_sft([attempt], secrets=(secret,))
    assert manifest['dropped_secret'] == ['escaped']
    assert manifest['sources'] == []


def test_sft_rows_carry_tool_calls_tool_results_artifacts_trust_and_redaction() -> None:
    messages, share, _ = export_sft(
        [_attempt('pass', trust='untrusted')], redaction={'digest': 'd' * 64, 'rules_version': 2}
    )
    [row] = [json.loads(line) for line in messages.splitlines()]
    assert list(row) == ['id', 'messages', 'artifacts', 'untrusted', 'redaction']
    assert row['id'] == 'try-1'
    assert row['untrusted'] is True
    assert row['redaction']['digest'] == 'd' * 64
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


def test_three_parallel_calls_keep_their_order_in_sft_and_sharegpt() -> None:
    calls = (Call('c-b', 'Read', '{"path": "b"}'), Call('c-c', 'Read', '{"path": "c"}'))
    calls = (Call('c-a', 'Write', '{"path": "a"}'), *calls)
    attempt = Attempt(
        'many',
        'g',
        'r',
        (Message('user', 'go'), Message('assistant', '', tool_calls=calls)),
        'pass',
    )
    messages, share, _ = export_sft([attempt])
    [row] = [json.loads(line) for line in messages.splitlines()]
    assert [call['id'] for call in row['messages'][1]['tool_calls']] == ['c-a', 'c-b', 'c-c']
    [shared] = [json.loads(line) for line in share.splitlines()]
    names = [json.loads(item['value'])['name'] for item in shared['conversations'][1:]]
    assert names == ['Write', 'Read', 'Read']


def test_include_unverified_rejects_a_yaml_bool_lookalike() -> None:
    with pytest.raises(ConfigError, match='include_unverified'):
        export_sft([_attempt('pass')], include_unverified=1)  # type: ignore[arg-type]


def test_one_bad_attempt_does_not_drop_the_rest() -> None:
    bad = Attempt('bad', 'goal-planets', 'rubric-1', (Message('nope', 'x'),), 'pass')
    messages, _, manifest = export_sft([bad, _attempt('pass', ident='ok')])
    assert '"ok"' in messages
    assert manifest['failed']
    assert 'bad' in str(manifest['failed'])


@pytest.mark.parametrize(
    'bad',
    [
        Attempt('', 'g', 'r', (), 'pass'),
        Attempt('x', 'g', 'r', (Message('user', 'x', tool_calls=(Call('c', 'Write'),)),), 'pass'),
        Attempt('x', 'g', 'r', (Message('user', 'x', trust='maybe'),), 'pass'),
        Attempt('x', 'g', 'r', (), 'pass', verdicts=('pass', 'block')),
        Attempt('x', 'g', 'r', (), 'pass', verdict_source='guess'),
    ],
    ids=['empty-id', 'user-tool-call', 'trust', 'verdict-history', 'verdict-source'],
)
def test_an_inconsistent_attempt_is_refused(bad: Attempt) -> None:
    with pytest.raises(ConfigError):
        trajectory(bad)


def test_preference_rows_are_dpo_shaped_and_come_from_the_same_goal_and_rubric() -> None:
    other = Attempt('other', 'goal-other', 'rubric-1', (Message('user', 'elsewhere'),), 'block')
    body, manifest = export_preference(
        [
            _attempt('pass', ident='chosen', content='good note'),
            _attempt('block', ident='rejected', content='bad note'),
            other,
            _attempt('unverified', ident='hidden'),
        ],
        include_unverified=True,
    )
    [row] = [json.loads(line) for line in body.splitlines()]
    assert (row['goal_id'], row['rubric_id']) == ('goal-planets', 'rubric-1')
    assert (row['chosen_id'], row['chosen_verdict']) == ('chosen', 'pass')
    assert (row['rejected_id'], row['rejected_verdict']) == ('rejected', 'block')
    assert row['prompt'] == [{'role': 'user', 'content': 'write planets.md'}]
    assert row['chosen'][0]['content'] == 'good note'
    assert row['rejected'][0]['content'] == 'bad note'
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


@pytest.mark.parametrize(
    ('verdict', 'reward'),
    [('pass', 1.0), ('partial', 0.5), ('block', 0.0), ('unverified', None)],
)
def test_the_reward_follows_the_verdict_and_unverified_is_not_a_failure(
    verdict: str, reward: float | None
) -> None:
    assert rl_row(_attempt(verdict), lambda text: [1])['reward'] == reward


def test_exporting_hands_back_copies_the_caller_cannot_use_to_change_the_attempt() -> None:
    attempt = _attempt('pass')
    document = trajectory(attempt)
    artifacts = document['artifacts']
    assert isinstance(artifacts, list)
    artifacts[0]['path'] = 'changed.md'
    assert attempt.artifacts[0]['path'] == 'planets.md'


@pytest.mark.parametrize(
    ('fmt', 'name', 'content_of'),
    [
        ('sft', 'messages.jsonl', lambda row: row['messages'][1]['content']),
        ('sharegpt', 'sharegpt.jsonl', lambda row: row['conversations'][1]['value']),
        ('trajectory', 'trajectories.jsonl', lambda row: row['messages'][1]['content']),
    ],
    ids=['sft', 'sharegpt', 'trajectory'],
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


def test_write_export_redacts_tool_arguments_artifacts_and_the_system_prompt(
    tmp_path: Path,
) -> None:
    attempt = Attempt(
        'leafy',
        'g',
        'r',
        (
            Message('user', 'go'),
            Message('assistant', '', tool_calls=(Call('c1', 'Write', f'{{"t": "{_SECRET}"}}'),)),
        ),
        'pass',
        artifacts=({'path': f'{_SECRET}.md', 'meta': {'note': _SECRET}},),
        system_prompt=f'key {_SECRET}',
    )
    write_export(
        [attempt], 'trajectory', tmp_path / 'out', redactor=RuleRedactor(secrets=[_SECRET])
    )
    [row] = [
        json.loads(line)
        for line in (tmp_path / 'out' / 'trajectories.jsonl').read_text().splitlines()
    ]
    assert _SECRET not in json.dumps(row)
    assert row['messages'][1]['tool_calls'][0]['arguments'].startswith('{"t": "[REDACTED:')
    assert row['artifacts'][0]['path'].startswith('[REDACTED:')
    assert row['artifacts'][0]['meta']['note'].startswith('[REDACTED:')
    assert row['system_prompt'].startswith('key [REDACTED:')


def test_a_quoted_secret_inside_tool_arguments_is_redacted_whole(tmp_path: Path) -> None:
    arguments = json.dumps({'token': _QUOTED_SECRET, 'path': 'a.md'})
    attempt = _attempt('pass', arguments=arguments)
    write_export(
        [attempt], 'trajectory', tmp_path / 'out', redactor=RuleRedactor(secrets=[_QUOTED_SECRET])
    )
    [row] = [
        json.loads(line)
        for line in (tmp_path / 'out' / 'trajectories.jsonl').read_text().splitlines()
    ]
    parsed = json.loads(row['messages'][1]['tool_calls'][0]['arguments'])
    assert parsed == {'token': '[REDACTED:secret.env_value]', 'path': 'a.md'}


def test_a_quoted_secret_inside_an_artifact_field_is_redacted_whole(tmp_path: Path) -> None:
    attempt = Attempt(
        'art',
        'g',
        'r',
        (Message('user', 'go'),),
        'pass',
        artifacts=({'path': f'{_QUOTED_SECRET}.md', 'meta': {'note': _QUOTED_SECRET}},),
    )
    write_export(
        [attempt], 'trajectory', tmp_path / 'out', redactor=RuleRedactor(secrets=[_QUOTED_SECRET])
    )
    [row] = [
        json.loads(line)
        for line in (tmp_path / 'out' / 'trajectories.jsonl').read_text().splitlines()
    ]
    assert row['artifacts'] == [
        {
            'path': '[REDACTED:secret.env_value].md',
            'meta': {'note': '[REDACTED:secret.env_value]'},
        }
    ]


def test_an_attempt_that_only_fails_while_encoding_is_reported_in_the_manifest(
    tmp_path: Path,
) -> None:
    unencodable = Attempt(
        'odd', 'g', 'r', (Message('user', 'go'),), 'pass', artifacts=({'tags': {'a'}},)
    )
    manifest = write_export([unencodable, _attempt('pass', ident='ok')], 'sft', tmp_path / 'out')
    assert manifest['sources'] == ['ok']
    failed = manifest['failed']
    assert isinstance(failed, list)
    assert failed == ['odd: TypeError']


def test_the_writer_never_replaces_or_follows_an_existing_name(tmp_path: Path) -> None:
    from syndica.runner import training

    existing = tmp_path / 'messages.jsonl'
    existing.write_text('keep', encoding='utf-8')
    target = tmp_path / 'elsewhere.txt'
    target.write_text('untouched', encoding='utf-8')
    link = tmp_path / 'manifest.json'
    link.symlink_to(target)
    for path in (existing, link):
        with pytest.raises(FileExistsError):
            training._write_new(path, 'new', RuleRedactor())  # pyright: ignore[reportPrivateUsage]
    assert existing.read_text() == 'keep'
    assert target.read_text() == 'untouched'


def test_write_export_rl_tokenizes_the_redacted_text_and_names_the_tokenizer(
    tmp_path: Path,
) -> None:
    attempt = _attempt('pass', content=f'note {_QUOTED_SECRET}')
    redactor = RuleRedactor(secrets=[_QUOTED_SECRET])
    manifest = write_export(
        [attempt],
        'rl',
        tmp_path / 'out',
        redactor=redactor,
        tokenize=lambda text: [len(text)],
        tokenizer='lengths',
    )
    [row] = [json.loads(line) for line in (tmp_path / 'out' / 'rl.jsonl').read_text().splitlines()]
    assert row['input_ids'][1] == len(_REDACTED_NOTE)
    assert manifest['tokenizer'] == 'lengths'


def test_write_export_records_the_redactor_it_used(tmp_path: Path) -> None:
    redactor = RuleRedactor()
    manifest = write_export([_attempt('pass')], 'sft', tmp_path / 'out', redactor=redactor)
    assert manifest['redaction'] == {
        'digest': redactor.digest,
        'rules_version': RULES_VERSION,
        'placeholders_kept': True,
        'raw_secrets_exported': False,
    }
    on_disk = json.loads((tmp_path / 'out' / 'manifest.json').read_text())
    assert on_disk['redaction']['digest'] == redactor.digest


def test_write_export_files_are_private_and_new(tmp_path: Path) -> None:
    write_export([_attempt('pass')], 'sft', tmp_path / 'out')
    for name in ('messages.jsonl', 'manifest.json'):
        assert stat.S_IMODE(os.stat(tmp_path / 'out' / name).st_mode) == 0o600


def test_write_export_refuses_a_directory_that_already_holds_an_export(tmp_path: Path) -> None:
    write_export([_attempt('unverified')], 'sft', tmp_path / 'out', include_unverified=True)
    before = (tmp_path / 'out' / 'manifest.json').read_text()
    with pytest.raises(ConfigError, match='空目录'):
        write_export([_attempt('pass')], 'rl', tmp_path / 'out', tokenize=lambda text: [1])
    assert (tmp_path / 'out' / 'manifest.json').read_text() == before
    assert not (tmp_path / 'out' / 'rl.jsonl').exists()


def test_write_export_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    target = tmp_path / 'elsewhere'
    target.mkdir()
    (tmp_path / 'out').symlink_to(target)
    with pytest.raises(ConfigError, match='符号链接'):
        write_export([_attempt('pass')], 'sft', tmp_path / 'out')
    assert list(target.iterdir()) == []


def test_write_export_rl_without_a_tokenizer_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match='tokenize'):
        write_export([_attempt('pass')], 'rl', tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_an_attempt_that_cannot_be_redacted_does_not_drop_the_rest(tmp_path: Path) -> None:
    broken = Attempt('broken', 'g', 'r', (Message('user', None),), 'pass')  # type: ignore[arg-type]
    manifest = write_export([broken, _attempt('pass', ident='ok')], 'sft', tmp_path / 'out')
    assert manifest['sources'] == ['ok']
    assert any(str(item).startswith('broken:') for item in manifest['failed'])  # type: ignore[union-attr]


def test_every_format_is_writable(tmp_path: Path) -> None:
    for fmt in FORMATS:
        manifest = write_export(
            [_attempt('pass'), _attempt('block', ident='worse')],
            fmt,
            tmp_path / fmt,
            tokenize=lambda text: [len(text)],
        )
        assert manifest['format'] == fmt
