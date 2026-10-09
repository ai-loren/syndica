"""训练出口的中立轨迹。不含分词器；TRL 只消费这里交出的列。

`unverified` 默认不进数据集。占位符 `[REDACTED:…]` 原样保留。调用方交来的原文密钥若还在样本里，
该样本不写出。导出只返回新对象，不改调用方交来的尝试。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from syndica.kernel import ConfigError, Redactor, RuleRedactor

__all__ = [
    'SCHEMA',
    'Attempt',
    'Call',
    'Message',
    'export_preference',
    'export_sft',
    'rl_row',
    'trajectory',
    'write_export',
]

SCHEMA = 'syndica.trajectory/1'
_DOCUMENT_KEYS = (
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
)
_TIMING_KEYS = ('start_ns', 'end_ns', 'cost')
_EDGE_KEYS = ('kind', 'source_span_id', 'target_span_id')
_MESSAGE_KEYS = ('role', 'content', 'tool_call_id', 'tool_calls')
_CALL_KEYS = ('id', 'name', 'arguments')
_REDACTION_KEYS = ('placeholders_kept', 'raw_secrets_exported')
_VERDICTS = frozenset({'pass', 'partial', 'block', 'unverified'})
_RANK = {'pass': 3, 'partial': 2, 'block': 1}
_ROLES = frozenset({'user', 'assistant', 'tool'})
_SHARE_FROM = {'user': 'human', 'assistant': 'gpt', 'tool': 'observation'}
_FORMATS = frozenset({'sft', 'sharegpt', 'preference', 'rl'})


@dataclass(frozen=True)
class Call:
    """assistant 发起的一次工具调用。`arguments` 是会话里存下的 JSON 文本（已脱敏）。"""

    id: str
    name: str
    arguments: str = '{}'


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[Call, ...] = ()


@dataclass(frozen=True)
class Attempt:
    """一次可导出的尝试。`verdict` 是四值之一。消息与产物在导出时复制。"""

    id: str
    goal_id: str
    rubric_id: str
    messages: tuple[Message, ...]
    verdict: str
    artifacts: tuple[Mapping[str, object], ...] = ()
    # (kind, source_span_id, target_span_id)。空元组表示这份轨迹没有因果边。
    causality: tuple[tuple[str, str, str], ...] = ()
    # (start_ns, end_ns, cost)。None 表示没有 span 可取时序。
    timing: tuple[int, int, str | None] | None = None


def trajectory(attempt: Attempt) -> dict[str, object]:
    """一份中立轨迹。键序固定，缺字段就报，不写一半。顶层 `tool_calls` 是按出现顺序的索引。"""
    _check_attempt(attempt)
    redaction = {key: value for key, value in zip(_REDACTION_KEYS, (True, False), strict=True)}
    fields: dict[str, object] = {
        'schema': SCHEMA,
        'id': attempt.id,
        'goal_id': attempt.goal_id,
        'rubric_id': attempt.rubric_id,
        'messages': [_message(item) for item in attempt.messages],
        'tool_calls': [
            {'id': call.id, 'name': call.name}
            for message in attempt.messages
            for call in message.tool_calls
        ],
        'artifacts': [dict(item) for item in attempt.artifacts],
        'verdict': attempt.verdict,
        'causality': [_edge(item) for item in attempt.causality],
        'timing': _timing(attempt.timing),
        'redaction': redaction,
    }
    return {key: fields[key] for key in _DOCUMENT_KEYS}


def export_sft(
    attempts: Sequence[Attempt],
    *,
    include_unverified: bool = False,
    secrets: Sequence[str] = (),
) -> tuple[str, str, dict[str, object]]:
    """SFT 的 messages jsonl（OpenAI 消息形状，带工具调用）、ShareGPT jsonl，以及 manifest。

    每行带 `id` 与 `artifacts`，样本能追回它出自哪次尝试、引用了哪些产物版本。
    一条尝试建轨迹失败或含未脱敏原文时跳过，其余照常写出。
    """
    _check_flag(include_unverified)
    kept, manifest = _select(attempts, include_unverified=include_unverified, secrets=secrets)
    messages = ''.join(
        json.dumps(
            {
                'id': attempt.id,
                'messages': [_openai(item) for item in attempt.messages],
                'artifacts': document['artifacts'],
            },
            ensure_ascii=False,
        )
        + '\n'
        for attempt, document in kept
    )
    share = ''.join(
        json.dumps(
            {'id': attempt.id, 'conversations': _share(attempt.messages)}, ensure_ascii=False
        )
        + '\n'
        for attempt, _ in kept
    )
    return messages, share, manifest


def export_preference(
    attempts: Sequence[Attempt],
    *,
    include_unverified: bool = False,
    secrets: Sequence[str] = (),
) -> tuple[str, dict[str, object]]:
    """同一 `goal_id` 与 `rubric_id` 下，裁决更高的一份为 chosen，更低的为 rejected。"""
    _check_flag(include_unverified)
    kept, manifest = _select(attempts, include_unverified=include_unverified, secrets=secrets)
    groups: dict[tuple[str, str], list[Attempt]] = {}
    for attempt, _ in kept:
        groups.setdefault((attempt.goal_id, attempt.rubric_id), []).append(attempt)
    lines: list[str] = []
    for (goal_id, rubric_id), items in groups.items():
        ranked = [item for item in items if item.verdict in _RANK]
        if len({item.verdict for item in ranked}) < 2:
            continue
        chosen = max(ranked, key=lambda item: _RANK[item.verdict])
        rejected = min(ranked, key=lambda item: _RANK[item.verdict])
        lines.append(
            json.dumps(
                {
                    'goal_id': goal_id,
                    'rubric_id': rubric_id,
                    'chosen': {'id': chosen.id, 'verdict': chosen.verdict},
                    'rejected': {'id': rejected.id, 'verdict': rejected.verdict},
                },
                ensure_ascii=False,
            )
        )
    body = ''.join(line + '\n' for line in lines)
    manifest['preference_pairs'] = len(lines)
    return body, manifest


def rl_row(attempt: Attempt, tokenize: object) -> dict[str, object]:
    """RL 一行：token id、loss mask、reward、episode 元数据。

    `tokenize` 只负责把字符串变成 int 列表。assistant 的正文与它发起的工具调用记 1，其余记 0。
    """
    document = trajectory(attempt)
    if not callable(tokenize):
        raise ConfigError(
            expected='tokenize 是把 str 变成 list[int] 的函数',
            received=f'收到 {type(tokenize).__name__}',
            next_action='传入 tokenize(text) -> list[int]；中立轨迹本身不含分词器',
        )
    input_ids: list[int] = []
    loss_mask: list[int] = []
    for message in attempt.messages:
        pieces = [message.content]
        if message.tool_calls:
            pieces.append(
                json.dumps([_call(call) for call in message.tool_calls], ensure_ascii=False)
            )
        for piece in pieces:
            tokens = _ints(tokenize(piece))
            if tokens is None:
                raise ConfigError(
                    expected='tokenize 返回 list[int]',
                    received='返回值里有 bool 或非 int',
                    next_action='不要返回 bool 或字符串；bool 是 int 的子类，这里单独拒绝',
                )
            input_ids.extend(tokens)
            loss_mask.extend([1 if message.role == 'assistant' else 0] * len(tokens))
    reward = {'pass': 1.0, 'partial': 0.5, 'block': 0.0, 'unverified': 0.0}[attempt.verdict]
    return {
        'input_ids': input_ids,
        'loss_mask': loss_mask,
        'reward': reward,
        'episode': {
            'id': document['id'],
            'goal_id': document['goal_id'],
            'rubric_id': document['rubric_id'],
            'verdict': document['verdict'],
        },
    }


def write_export(
    attempts: Sequence[Attempt],
    fmt: str,
    output: Path,
    *,
    include_unverified: bool = False,
    secrets: Sequence[str] = (),
    tokenize: object = None,
    failed: Sequence[str] = (),
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """把尝试写成 `output` 目录里的 jsonl 与 manifest。不改 `attempts`。

    `rl` 要调用方给 `tokenize`：中立轨迹不含分词器。一条失败记进 manifest，其余照写。
    """
    _check_flag(include_unverified)
    if fmt not in _FORMATS:
        raise ConfigError(
            expected=f'format 是 {sorted(_FORMATS)} 之一',
            received=f'format={fmt!r}',
            next_action='用 sft、sharegpt、preference 或 rl',
        )
    if not isinstance(cast(object, output), Path):
        raise ConfigError(
            expected='output 是目录路径',
            received=f'收到 {type(output).__name__}',
            next_action='传 pathlib.Path；导出写在这个目录里，不改源 trace',
        )
    if output.exists() and not output.is_dir():
        raise ConfigError(
            expected=f'{output} 是目录，或者还不存在',
            received='它已经是一个文件',
            next_action='换一个目录路径，或删掉这个文件后再导出',
        )
    if fmt == 'rl' and not callable(tokenize):
        raise ConfigError(
            expected='export rl 的 tokenize 是把 str 变成 list[int] 的函数',
            received=f'收到 {type(tokenize).__name__}',
            next_action='传入 tokenize；token id 属于训练 backend，不写进中立轨迹',
        )
    if redactor is None:
        redactor = RuleRedactor()
    elif not isinstance(cast(object, redactor), Redactor):
        raise ConfigError(
            expected='redactor 是 Redactor',
            received=f'收到 {type(redactor).__name__}',
            next_action='传 RuleRedactor；导出落盘前还要再过一遍脱敏',
        )
    output.mkdir(parents=True, exist_ok=True)
    # 先逐叶脱敏再编码：编码之后转义过的引号与反斜杠会让规则只认出密钥的一截。
    attempts = [_redacted(attempt, redactor) for attempt in attempts]
    if fmt == 'preference':
        body, manifest = export_preference(
            attempts, include_unverified=include_unverified, secrets=secrets
        )
        name = 'preference.jsonl'
    elif fmt == 'rl':
        kept, manifest = _select(attempts, include_unverified=include_unverified, secrets=secrets)
        body = ''.join(
            json.dumps(rl_row(attempt, tokenize), ensure_ascii=False) + '\n' for attempt, _ in kept
        )
        name = 'rl.jsonl'
    else:
        messages, share, manifest = export_sft(
            attempts, include_unverified=include_unverified, secrets=secrets
        )
        body = share if fmt == 'sharegpt' else messages
        name = 'sharegpt.jsonl' if fmt == 'sharegpt' else 'messages.jsonl'
    prior = manifest.get('failed')
    earlier = [str(item) for item in cast(list[object], prior)] if isinstance(prior, list) else []
    merged: list[str] = [*earlier, *failed]
    manifest['failed'] = merged
    manifest['format'] = fmt
    (output / name).write_text(redactor.redact(body), encoding='utf-8')
    (output / 'manifest.json').write_text(
        redactor.redact(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'),
        encoding='utf-8',
    )
    return manifest


def _redacted(attempt: Attempt, redactor: Redactor) -> Attempt:
    """正文、工具实参与产物字段逐个脱敏。id、角色与工具名是结构，不动。"""
    if not isinstance(cast(object, attempt), Attempt):
        return attempt
    messages = tuple(
        replace(
            message,
            content=redactor.redact(message.content),
            tool_calls=tuple(
                replace(call, arguments=redactor.redact(call.arguments))
                for call in message.tool_calls
            ),
        )
        for message in attempt.messages
    )
    artifacts = tuple(
        {
            key: redactor.redact(value) if isinstance(value, str) else value
            for key, value in item.items()
        }
        for item in attempt.artifacts
    )
    return replace(attempt, messages=messages, artifacts=artifacts)


def _select(
    attempts: Sequence[Attempt],
    *,
    include_unverified: bool,
    secrets: Sequence[str],
) -> tuple[list[tuple[Attempt, dict[str, object]]], dict[str, object]]:
    kept: list[tuple[Attempt, dict[str, object]]] = []
    dropped_unverified: list[str] = []
    dropped_secret: list[str] = []
    failed: list[str] = []
    for attempt in attempts:
        try:
            document = trajectory(attempt)
        except ConfigError as exc:
            failed.append(f'{getattr(attempt, "id", "?")}: {exc.expected}')
            continue
        if attempt.verdict == 'unverified' and not include_unverified:
            dropped_unverified.append(attempt.id)
            continue
        rendered = json.dumps(document, ensure_ascii=False)
        if any(secret and secret in rendered for secret in secrets):
            dropped_secret.append(attempt.id)
            continue
        kept.append((attempt, document))
    manifest: dict[str, object] = {
        'schema': SCHEMA,
        'include_unverified': include_unverified,
        'sources': [attempt.id for attempt, _ in kept],
        'dropped_unverified': dropped_unverified,
        'dropped_secret': dropped_secret,
        'failed': failed,
        'redaction': {'placeholders_kept': True, 'raw_secrets_exported': False},
    }
    return kept, manifest


def _check_flag(include_unverified: object) -> None:
    if not isinstance(include_unverified, bool):
        raise ConfigError(
            expected='include_unverified 是布尔值',
            received=f'收到 {include_unverified!r}',
            next_action='显式传 True 才把 unverified 写进数据集；不要用 yes / 1',
        )


def _ints(value: object) -> list[int] | None:
    if not isinstance(value, list):
        return None
    found: list[int] = []
    for item in cast(list[object], value):
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        found.append(item)
    return found


def _check_attempt(attempt: object) -> None:
    if not isinstance(attempt, Attempt):
        raise ConfigError(
            expected='尝试是 Attempt',
            received=f'收到 {type(attempt).__name__}',
            next_action='用 Attempt(id, goal_id, rubric_id, messages, verdict) 构造',
        )
    if attempt.verdict not in _VERDICTS:
        raise ConfigError(
            expected=f'verdict 是 {sorted(_VERDICTS)} 之一',
            received=f'verdict={attempt.verdict!r}',
            next_action='用 pass、partial、block 或 unverified',
        )
    for message in attempt.messages:
        if message.role not in _ROLES:
            raise ConfigError(
                expected='消息 role 是 user、assistant 或 tool',
                received=f'role={message.role!r}',
                next_action='工具结果用 role=tool',
            )
        if message.tool_calls and message.role != 'assistant':
            raise ConfigError(
                expected='只有 assistant 消息带 tool_calls',
                received=f'role={message.role!r} 的消息带了 {len(message.tool_calls)} 个工具调用',
                next_action='把工具调用挂在发起它的 assistant 消息上，结果用 role=tool',
            )


def _edge(item: tuple[str, str, str]) -> dict[str, object]:
    fields = {'kind': item[0], 'source_span_id': item[1], 'target_span_id': item[2]}
    return {key: fields[key] for key in _EDGE_KEYS}


def _timing(value: tuple[int, int, str | None] | None) -> dict[str, object]:
    start, end, cost = (None, None, None) if value is None else value
    fields: dict[str, object] = {'start_ns': start, 'end_ns': end, 'cost': cost}
    return {key: fields[key] for key in _TIMING_KEYS}


def _call(call: Call) -> dict[str, object]:
    fields: dict[str, object] = {'id': call.id, 'name': call.name, 'arguments': call.arguments}
    return {key: fields[key] for key in _CALL_KEYS}


def _message(message: Message) -> dict[str, object]:
    fields: dict[str, object] = {
        'role': message.role,
        'content': message.content,
        'tool_call_id': message.tool_call_id,
        'tool_calls': [_call(call) for call in message.tool_calls],
    }
    return {key: fields[key] for key in _MESSAGE_KEYS}


def _openai(message: Message) -> dict[str, object]:
    row: dict[str, object] = {'role': message.role, 'content': message.content}
    if message.role == 'tool':
        row['tool_call_id'] = message.tool_call_id
    if message.tool_calls:
        row['tool_calls'] = [
            {
                'id': call.id,
                'type': 'function',
                'function': {'name': call.name, 'arguments': call.arguments},
            }
            for call in message.tool_calls
        ]
    return row


def _share(messages: Sequence[Message]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for message in messages:
        if message.content or not message.tool_calls:
            rows.append({'from': _SHARE_FROM[message.role], 'value': message.content})
        for call in message.tool_calls:
            value = json.dumps({'name': call.name, 'arguments': call.arguments}, ensure_ascii=False)
            rows.append({'from': 'function_call', 'value': value})
    return rows
