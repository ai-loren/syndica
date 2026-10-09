"""训练出口的中立轨迹。不含分词器；TRL 只消费这里交出的列。

`unverified` 默认不进数据集。占位符 `[REDACTED:…]` 原样保留。调用方点名的原文密钥若还在样本的任何
一个字符串里，该样本不写出。导出只返回新对象，不改调用方交来的尝试。
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from syndica.kernel import ConfigError, Redactor, RuleRedactor
from syndica.kernel.redaction import RULES_VERSION, write_all

__all__ = [
    'FORMATS',
    'SCHEMA',
    'Attempt',
    'Call',
    'Message',
    'export_preference',
    'export_sft',
    'read_trajectory',
    'rl_row',
    'trajectory',
    'write_export',
]

SCHEMA = 'syndica.trajectory/1'
FORMATS = ('sft', 'sharegpt', 'preference', 'rl', 'trajectory')
_DOCUMENT_KEYS = (
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
)
_TIMING_KEYS = ('start_ns', 'end_ns', 'cost')
_EDGE_KEYS = ('kind', 'source_span_id', 'target_span_id')
_SOURCE_KEYS = ('trace_id', 'verdict_source')
_MESSAGE_KEYS = ('role', 'content', 'tool_call_id', 'tool_calls', 'trust')
_CALL_KEYS = ('id', 'name', 'arguments')
_REDACTION_KEYS = ('digest', 'rules_version', 'placeholders_kept', 'raw_secrets_exported')
_VERDICTS = frozenset({'pass', 'partial', 'block', 'unverified'})
_VERDICT_SOURCES = frozenset({'acceptance', 'none', 'missing'})
_TRUST = frozenset({'trusted', 'untrusted'})
_RANK = {'pass': 3, 'partial': 2, 'block': 1}
# unverified 的 reward 是 None：「没能验证」不是「失败」，训练框架那一侧自己决定怎么用它。
_REWARD: Mapping[str, float | None] = {
    'pass': 1.0,
    'partial': 0.5,
    'block': 0.0,
    'unverified': None,
}
_ROLES = frozenset({'user', 'assistant', 'tool'})
_SHARE_FROM = {'user': 'human', 'assistant': 'gpt', 'tool': 'observation'}
_FILES = {
    'sft': 'messages.jsonl',
    'sharegpt': 'sharegpt.jsonl',
    'preference': 'preference.jsonl',
    'rl': 'rl.jsonl',
    'trajectory': 'trajectories.jsonl',
}


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
    trust: str = 'trusted'


@dataclass(frozen=True)
class Attempt:
    """一次可导出的尝试。`verdict` 是四值之一，`verdicts` 是同一会话里根 Agent 先后得到的全部裁决
    （验收打回之后的重试也在这条会话里），最后一个与 `verdict` 相同。消息与产物在导出时复制。"""

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
    model: str = ''
    system_prompt: str = ''
    tools: tuple[str, ...] = ()
    verdicts: tuple[str, ...] = ()
    # 逐条 Rubric 的结论，形如 `R1=satisfied`。
    outcomes: tuple[str, ...] = ()
    trace_id: str | None = None
    # acceptance：trace 上有裁决；none：有 trace 但没有裁决；missing：找不到 trace。
    verdict_source: str = 'acceptance'


def trajectory(
    attempt: Attempt, *, redaction: Mapping[str, object] | None = None
) -> dict[str, object]:
    """一份中立轨迹。键序固定，缺字段或取值越界就报，不写一半。`redaction` 是导出时用的脱敏器的
    摘要与规则版本；不经脱敏器的调用给 None。"""
    _check_attempt(attempt)
    given = dict(redaction) if redaction is not None else {}
    meta: dict[str, object] = {
        'digest': given.get('digest'),
        'rules_version': given.get('rules_version'),
        'placeholders_kept': True,
        'raw_secrets_exported': False,
    }
    fields: dict[str, object] = {
        'schema': SCHEMA,
        'id': attempt.id,
        'goal_id': attempt.goal_id,
        'rubric_id': attempt.rubric_id,
        'model': attempt.model,
        'system_prompt': attempt.system_prompt,
        'tools': list(attempt.tools),
        'messages': [_message(item) for item in attempt.messages],
        'tool_calls': [
            {'id': call.id, 'name': call.name}
            for message in attempt.messages
            for call in message.tool_calls
        ],
        'artifacts': [_copied(item) for item in attempt.artifacts],
        'verdict': attempt.verdict,
        'verdicts': list(attempt.verdicts) or [attempt.verdict],
        'outcomes': list(attempt.outcomes),
        'source': _ordered(
            {'trace_id': attempt.trace_id, 'verdict_source': attempt.verdict_source},
            _SOURCE_KEYS,
        ),
        'causality': [_edge(item) for item in attempt.causality],
        'timing': _timing(attempt.timing),
        'redaction': _ordered(meta, _REDACTION_KEYS),
    }
    return _ordered(fields, _DOCUMENT_KEYS)


def read_trajectory(document: object) -> dict[str, object]:
    """读一份中立轨迹。只认 `syndica.trajectory/1`；更新的版本三件套拒绝，不按旧版猜。"""
    if not isinstance(document, Mapping):
        raise ConfigError(
            expected=f'中立轨迹是 JSON 对象，schema 是 {SCHEMA}',
            received=f'收到 {type(document).__name__}',
            next_action='传 trajectories.jsonl 里的一行解析出来的对象',
        )
    row = cast(Mapping[str, object], document)
    schema = row.get('schema')
    if schema != SCHEMA:
        raise ConfigError(
            expected=f'schema 是 {SCHEMA}',
            received=f'schema={schema!r}',
            next_action='用写出这份数据的 syndica 版本读，或升级 syndica',
        )
    missing = [key for key in _DOCUMENT_KEYS if key not in row]
    if missing:
        raise ConfigError(
            expected=f'中立轨迹带齐 {", ".join(_DOCUMENT_KEYS)}',
            received=f'缺 {", ".join(missing)}',
            next_action='重新 syndica export trajectory；手改过的行不要再交进来',
        )
    return {key: row[key] for key in _DOCUMENT_KEYS}


def export_sft(
    attempts: Sequence[Attempt],
    *,
    include_unverified: bool = False,
    secrets: Sequence[str] = (),
    redaction: Mapping[str, object] | None = None,
) -> tuple[str, str, dict[str, object]]:
    """SFT 的 messages jsonl（OpenAI 消息形状，带工具调用）、ShareGPT jsonl，以及 manifest。

    每行带 `id`、`artifacts`、`untrusted` 与 `redaction`，样本能追回它出自哪次尝试、引用了哪些产物
    版本、有没有不可信内容、用哪套规则脱敏。一条尝试建轨迹失败或含点名的原文密钥时跳过，其余照常。
    """
    _check_flag(include_unverified)
    kept, manifest = _select(
        attempts, include_unverified=include_unverified, secrets=secrets, redaction=redaction
    )
    messages = ''.join(
        json.dumps(
            {
                'id': attempt.id,
                'messages': [_openai(item) for item in attempt.messages],
                'artifacts': document['artifacts'],
                'untrusted': _untrusted(attempt),
                'redaction': document['redaction'],
            },
            ensure_ascii=False,
        )
        + '\n'
        for attempt, document in kept
    )
    share = ''.join(
        json.dumps(
            {
                'id': attempt.id,
                'conversations': _share(attempt.messages),
                'untrusted': _untrusted(attempt),
                'redaction': document['redaction'],
            },
            ensure_ascii=False,
        )
        + '\n'
        for attempt, document in kept
    )
    return messages, share, manifest


def export_preference(
    attempts: Sequence[Attempt],
    *,
    include_unverified: bool = False,
    secrets: Sequence[str] = (),
    redaction: Mapping[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """同一 `goal_id` 与 `rubric_id` 下，裁决更高的一份为 chosen，更低的为 rejected。

    行是对话形状的 `prompt` / `chosen` / `rejected`（TRL 的 DPO 直接读），另带两边的 id 与裁决。
    `prompt` 是 chosen 那一份第一条 assistant 之前的消息。unverified 不进配对，即使显式包含了它。
    """
    _check_flag(include_unverified)
    kept, manifest = _select(
        attempts, include_unverified=include_unverified, secrets=secrets, redaction=redaction
    )
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
        prompt, chosen_tail = _split_prompt(chosen.messages)
        _, rejected_tail = _split_prompt(rejected.messages)
        lines.append(
            json.dumps(
                {
                    'goal_id': goal_id,
                    'rubric_id': rubric_id,
                    'chosen_id': chosen.id,
                    'chosen_verdict': chosen.verdict,
                    'rejected_id': rejected.id,
                    'rejected_verdict': rejected.verdict,
                    'prompt': [_openai(item) for item in prompt],
                    'chosen': [_openai(item) for item in chosen_tail],
                    'rejected': [_openai(item) for item in rejected_tail],
                    'untrusted': _untrusted(chosen) or _untrusted(rejected),
                    'redaction': manifest['redaction'],
                },
                ensure_ascii=False,
            )
        )
    body = ''.join(line + '\n' for line in lines)
    manifest['preference_pairs'] = len(lines)
    return body, manifest


def rl_row(
    attempt: Attempt, tokenize: object, *, redaction: Mapping[str, object] | None = None
) -> dict[str, object]:
    """RL 一行：token id、loss mask、reward、episode 元数据。

    `tokenize` 只负责把字符串变成 int 列表。assistant 的正文与它发起的工具调用记 1，其余记 0。
    unverified 的 reward 是 None。
    """
    document = trajectory(attempt, redaction=redaction)
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
    return {
        'input_ids': input_ids,
        'loss_mask': loss_mask,
        'reward': _REWARD[attempt.verdict],
        'episode': {
            'id': document['id'],
            'goal_id': document['goal_id'],
            'rubric_id': document['rubric_id'],
            'verdict': document['verdict'],
            'verdicts': document['verdicts'],
            'untrusted': _untrusted(attempt),
        },
        'redaction': document['redaction'],
    }


def write_export(
    attempts: Sequence[Attempt],
    fmt: str,
    output: Path,
    *,
    include_unverified: bool = False,
    secrets: Sequence[str] = (),
    tokenize: object = None,
    tokenizer: str | None = None,
    failed: Sequence[str] = (),
    skipped_traces: Sequence[str] = (),
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """把尝试写成 `output` 目录里的一份 jsonl 与 `manifest.json`。不改 `attempts`。

    `output` 必须还不存在或是空目录、且不是符号链接：一个目录一次导出，manifest 不会被下一次导出
    覆盖成别的过滤条件。文件以 0600 新建，不跟随符号链接，不覆盖已有文件。
    `rl` 要调用方给 `tokenize`（与它的名字 `tokenizer`，记进 manifest）。一条失败记进 manifest，
    其余照写。
    """
    _check_flag(include_unverified)
    if fmt not in FORMATS:
        raise ConfigError(
            expected=f'format 是 {", ".join(FORMATS)} 之一',
            received=f'format={fmt!r}',
            next_action='用 sft、sharegpt、preference、rl 或 trajectory',
        )
    if not isinstance(cast(object, output), Path):
        raise ConfigError(
            expected='output 是目录路径',
            received=f'收到 {type(output).__name__}',
            next_action='传 pathlib.Path；导出写在这个目录里，不改源 trace',
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
            next_action='传宿主的 redactor_for(config)；导出落盘前要再过一遍同一套脱敏',
        )
    _prepare_directory(output)
    redaction: dict[str, object] = {
        'digest': redactor.digest if isinstance(redactor, RuleRedactor) else None,
        'rules_version': RULES_VERSION,
    }
    redacted, unreadable = _all_redacted(attempts, redactor)
    if fmt == 'preference':
        body, manifest = export_preference(
            redacted, include_unverified=include_unverified, secrets=secrets, redaction=redaction
        )
    elif fmt in ('rl', 'trajectory'):
        kept, manifest = _select(
            redacted, include_unverified=include_unverified, secrets=secrets, redaction=redaction
        )
        if fmt == 'rl':
            body = ''.join(
                json.dumps(rl_row(attempt, tokenize, redaction=redaction), ensure_ascii=False)
                + '\n'
                for attempt, _ in kept
            )
            manifest['tokenizer'] = tokenizer or getattr(tokenize, '__name__', 'unknown')
        else:
            body = ''.join(json.dumps(document, ensure_ascii=False) + '\n' for _, document in kept)
    else:
        messages, share, manifest = export_sft(
            redacted, include_unverified=include_unverified, secrets=secrets, redaction=redaction
        )
        body = share if fmt == 'sharegpt' else messages
    prior = manifest.get('failed')
    earlier = [str(item) for item in cast(list[object], prior)] if isinstance(prior, list) else []
    manifest['failed'] = [*unreadable, *earlier, *failed]
    manifest['skipped_traces'] = list(skipped_traces)
    manifest['format'] = fmt
    manifest['redaction'] = _ordered(
        {**redaction, 'placeholders_kept': True, 'raw_secrets_exported': False}, _REDACTION_KEYS
    )
    _write_new(output / _FILES[fmt], body, redactor)
    _write_new(
        output / 'manifest.json',
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        redactor,
    )
    return manifest


def _prepare_directory(output: Path) -> None:
    try:
        info = output.lstat()
    except FileNotFoundError:
        output.mkdir(parents=True, mode=0o700)
        return
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError(
            expected=f'{output} 是真目录或还不存在',
            received='它是符号链接',
            next_action='换一个新目录；导出不往链接指向的地方写',
        )
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError(
            expected=f'{output} 是目录或还不存在',
            received='它是一个文件',
            next_action='换一个新目录路径',
        )
    if any(output.iterdir()):
        raise ConfigError(
            expected=f'{output} 是空目录或还不存在',
            received='里面已经有文件',
            next_action='换一个新目录；一个目录一次导出，manifest 才对得上里面的数据',
        )


def _write_new(path: Path, text: str, redactor: Redactor) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        write_all(descriptor, redactor.redact(text).encode('utf-8'))
    finally:
        os.close(descriptor)


def _all_redacted(
    attempts: Sequence[Attempt], redactor: Redactor
) -> tuple[list[Attempt], list[str]]:
    """逐条脱敏。一条脱不了（类型不对、正文不是字符串）记进失败名单，其余照常。"""
    redacted: list[Attempt] = []
    failed: list[str] = []
    for attempt in attempts:
        try:
            _check_attempt(attempt)
            redacted.append(_redacted(attempt, redactor))
        except ConfigError as exc:
            failed.append(f'{getattr(attempt, "id", "?")}: {exc.expected}')
        except Exception as exc:
            failed.append(f'{getattr(attempt, "id", "?")}: {type(exc).__name__}')
    return redacted, failed


def _redacted(attempt: Attempt, redactor: Redactor) -> Attempt:
    """正文、工具实参、system prompt 与产物里的每个字符串逐个脱敏。id、角色与工具名是结构，不动。"""
    messages = tuple(
        replace(
            message,
            content=redactor.redact(message.content),
            tool_calls=tuple(
                replace(call, arguments=_redact_arguments(call.arguments, redactor))
                for call in message.tool_calls
            ),
        )
        for message in attempt.messages
    )
    artifacts = tuple(
        cast(Mapping[str, object], _redact_tree(dict(item), redactor)) for item in attempt.artifacts
    )
    return replace(
        attempt,
        messages=messages,
        artifacts=artifacts,
        system_prompt=redactor.redact(attempt.system_prompt),
    )


def _redact_arguments(text: str, redactor: Redactor) -> str:
    """工具实参是一段 JSON 文本：带引号或反斜杠的密钥在文本里是转义过的，按文本脱敏只认得出一截。
    先按原文解析：先做文本脱敏会把转义的引号后面换成占位符，留下 `\\[`，JSON 就坏了。能解析就逐叶
    脱敏，逐叶没有改动时保留原文、格式不变；最后整段再过一遍文本规则。"""
    try:
        parsed = cast(object, json.loads(text))
    except (json.JSONDecodeError, RecursionError):
        return redactor.redact(text)
    tree = _redact_tree(parsed, redactor)
    encoded = text if tree == parsed else json.dumps(tree, ensure_ascii=False)
    return redactor.redact(encoded)


def _redact_tree(value: object, redactor: Redactor) -> object:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, Mapping):
        return {
            key: _redact_tree(item, redactor)
            for key, item in cast(Mapping[str, object], value).items()
        }
    if isinstance(value, list | tuple):
        return [_redact_tree(item, redactor) for item in cast(Sequence[object], value)]
    return value


def _select(
    attempts: Sequence[Attempt],
    *,
    include_unverified: bool,
    secrets: Sequence[str],
    redaction: Mapping[str, object] | None,
) -> tuple[list[tuple[Attempt, dict[str, object]]], dict[str, object]]:
    kept: list[tuple[Attempt, dict[str, object]]] = []
    dropped_unverified: list[str] = []
    dropped_secret: list[str] = []
    failed: list[str] = []
    named = [secret for secret in secrets if secret]
    for attempt in attempts:
        try:
            document = trajectory(attempt, redaction=redaction)
        except ConfigError as exc:
            failed.append(f'{getattr(attempt, "id", "?")}: {exc.expected}')
            continue
        except Exception as exc:
            failed.append(f'{getattr(attempt, "id", "?")}: {type(exc).__name__}')
            continue
        if attempt.verdict == 'unverified' and not include_unverified:
            dropped_unverified.append(attempt.id)
            continue
        # 在解码后的每个字符串上查：在 JSON 文本上查，带引号、反斜杠或换行的密钥被转义后就认不出。
        # 工具实参本身又是一段 JSON 文本，解析之后再查一层。
        if any(secret in text for text in _sample_strings(attempt, document) for secret in named):
            dropped_secret.append(attempt.id)
            continue
        kept.append((attempt, document))
    given = dict(redaction) if redaction is not None else {}
    manifest: dict[str, object] = {
        'schema': SCHEMA,
        'include_unverified': include_unverified,
        'sources': [attempt.id for attempt, _ in kept],
        'samples': [
            {
                'id': attempt.id,
                'verdict': attempt.verdict,
                'verdicts': list(attempt.verdicts) or [attempt.verdict],
                'trace_id': attempt.trace_id,
                'verdict_source': attempt.verdict_source,
                'untrusted': _untrusted(attempt),
            }
            for attempt, _ in kept
        ],
        'dropped_unverified': dropped_unverified,
        'dropped_secret': dropped_secret,
        'failed': failed,
        'redaction': _ordered(
            {
                'digest': given.get('digest'),
                'rules_version': given.get('rules_version'),
                'placeholders_kept': True,
                'raw_secrets_exported': False,
            },
            _REDACTION_KEYS,
        ),
    }
    return kept, manifest


def _sample_strings(attempt: Attempt, document: Mapping[str, object]) -> Iterator[str]:
    yield from _strings(document)
    for message in attempt.messages:
        for call in message.tool_calls:
            try:
                yield from _strings(cast(object, json.loads(call.arguments)))
            except (json.JSONDecodeError, RecursionError):
                continue


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in cast(Sequence[object], value):
            yield from _strings(item)


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
    for field, value in (('id', attempt.id), ('goal_id', attempt.goal_id)):
        if not isinstance(cast(object, value), str) or not value:
            raise ConfigError(
                expected=f'Attempt.{field} 是非空字符串',
                received=f'{field}={value!r}',
                next_action=f'给这次尝试一个 {field}；空值会让样本追不回来源',
            )
    if attempt.verdict not in _VERDICTS:
        raise ConfigError(
            expected=f'verdict 是 {sorted(_VERDICTS)} 之一',
            received=f'verdict={attempt.verdict!r}',
            next_action='用 pass、partial、block 或 unverified',
        )
    if attempt.verdicts and attempt.verdicts[-1] != attempt.verdict:
        raise ConfigError(
            expected='verdicts 的最后一个就是 verdict',
            received=f'verdicts={list(attempt.verdicts)!r}，verdict={attempt.verdict!r}',
            next_action='verdicts 按先后排列，最后一个是这次会话最终的裁决',
        )
    if attempt.verdict_source not in _VERDICT_SOURCES:
        raise ConfigError(
            expected=f'verdict_source 是 {sorted(_VERDICT_SOURCES)} 之一',
            received=f'verdict_source={attempt.verdict_source!r}',
            next_action='trace 上有裁决用 acceptance，没有用 none，找不到 trace 用 missing',
        )
    for message in attempt.messages:
        if message.role not in _ROLES:
            raise ConfigError(
                expected='消息 role 是 user、assistant 或 tool',
                received=f'role={message.role!r}',
                next_action='工具结果用 role=tool',
            )
        if not isinstance(cast(object, message.content), str):
            raise ConfigError(
                expected='消息 content 是字符串',
                received=f'{message.role} 消息的 content 是 {type(message.content).__name__}',
                next_action='把多段内容拼成一个字符串；没有正文用空串',
            )
        if message.trust not in _TRUST:
            raise ConfigError(
                expected='消息 trust 是 trusted 或 untrusted',
                received=f'trust={message.trust!r}',
                next_action='沿用会话块上的 TrustLevel.value',
            )
        if message.tool_calls and message.role != 'assistant':
            raise ConfigError(
                expected='只有 assistant 消息带 tool_calls',
                received=f'role={message.role!r} 的消息带了 {len(message.tool_calls)} 个工具调用',
                next_action='把工具调用挂在发起它的 assistant 消息上，结果用 role=tool',
            )


def _ordered(fields: Mapping[str, object], keys: Sequence[str]) -> dict[str, object]:
    return {key: fields[key] for key in keys}


def _copied(item: Mapping[str, object]) -> dict[str, object]:
    return cast(dict[str, object], json.loads(json.dumps(dict(item), ensure_ascii=False)))


def _untrusted(attempt: Attempt) -> bool:
    return any(message.trust == 'untrusted' for message in attempt.messages)


def _split_prompt(messages: Sequence[Message]) -> tuple[list[Message], list[Message]]:
    for index, message in enumerate(messages):
        if message.role == 'assistant':
            return list(messages[:index]), list(messages[index:])
    return list(messages), []


def _edge(item: tuple[str, str, str]) -> dict[str, object]:
    fields = {'kind': item[0], 'source_span_id': item[1], 'target_span_id': item[2]}
    return _ordered(fields, _EDGE_KEYS)


def _timing(value: tuple[int, int, str | None] | None) -> dict[str, object]:
    start, end, cost = (None, None, None) if value is None else value
    fields: dict[str, object] = {'start_ns': start, 'end_ns': end, 'cost': cost}
    return _ordered(fields, _TIMING_KEYS)


def _call(call: Call) -> dict[str, object]:
    fields: dict[str, object] = {'id': call.id, 'name': call.name, 'arguments': call.arguments}
    return _ordered(fields, _CALL_KEYS)


def _message(message: Message) -> dict[str, object]:
    fields: dict[str, object] = {
        'role': message.role,
        'content': message.content,
        'tool_call_id': message.tool_call_id,
        'tool_calls': [_call(call) for call in message.tool_calls],
        'trust': message.trust,
    }
    return _ordered(fields, _MESSAGE_KEYS)


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
