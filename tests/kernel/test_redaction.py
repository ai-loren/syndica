"""内核统一脱敏器（spec §12.3、M3 D4）。

样例一律是运行时拼出的合成假值，不在源码里逐字出现：否则仓库自己的提交会撞上提交闸。
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import pickle
import time
import types
from pathlib import Path
from typing import Any, cast

import pytest

from syndica.kernel import ConfigError, ContractViolation, Finding, RedactionRule, RuleRedactor
from syndica.kernel.redaction import (
    _ASSIGNMENT,  # pyright: ignore[reportPrivateUsage]
    RULE_IDS,
    RULES_VERSION,
    RedactedText,
    _entropy,  # pyright: ignore[reportPrivateUsage]
    encode_digests,
    encode_prepared,
    encode_redacted,
    looks_secret,
    write_all,
    write_redacted,
)

_ALNUM = 'aZ3kQ9mX1bW7pL5nV2cR8tY4dS6fH0gJ'
_HEX = '0123456789abcdef'
_UPPER32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
_ROOT = Path(__file__).resolve().parents[2]


def _run(alphabet: str, length: int, seed: int) -> str:
    """定种子的伪随机串，取自 sha256 的输出：`random.Random(seed).choice` 的序列不保证跨 Python 版本
    不变，而黄金语料的摘要要跨版本稳定。按位置算出来的串是周期的（16 字符的片段会重复出现），这里
    不是。"""
    stream = b''.join(
        hashlib.sha256(f'{seed}:{block}'.encode()).digest() for block in range(length // 32 + 1)
    )
    return ''.join(alphabet[byte % len(alphabet)] for byte in stream[:length])


_OPENROUTER = 'sk-or-v1-' + _run(_HEX, 64, 1)
_OPENAI = 'sk-' + 'proj-' + _run(_ALNUM, 48, 2)
_GOOGLE = 'AI' + 'za' + _run(_ALNUM + '_-', 35, 3)
_GITHUB = 'gh' + 'p_' + _run(_ALNUM, 36, 4)
_GITHUB_PAT = 'github' + '_pat_' + _run(_ALNUM + '_', 82, 5)
_AWS = 'AK' + 'IA' + _run(_UPPER32, 16, 6)
_SLACK = 'xo' + 'xb-' + '123456789012-' + _run(_ALNUM, 24, 7)
_BEGIN = '-----BEGIN ' + 'OPENSSH PRIVATE' + ' KEY-----'
_END = '-----END ' + 'OPENSSH PRIVATE' + ' KEY-----'
_KEY_BODY = '\n'.join(_run(_ALNUM + '+/', 64, seed) for seed in (8, 9, 10))
_STRIPE = 'sk_' + 'live_' + _run(_ALNUM, 24, 40)
_AWS_SECRET = _run(_ALNUM, 20, 41) + '/' + _run(_ALNUM + '+', 19, 42)


def _redactor(*secrets: str, rules: tuple[RedactionRule, ...] = ()) -> RuleRedactor:
    return RuleRedactor(secrets=secrets, rules=rules)


def _placeholder(rule_id: str) -> str:
    return f'[REDACTED:{rule_id}]'


def test_the_rule_ids_keep_the_m2_ten_in_order_and_add_named_assignment_last():
    assert RULE_IDS == (
        'secret.env_value',
        'secret.private_key_block',
        'secret.authorization_header',
        'secret.url_userinfo',
        'secret.openrouter_key',
        'secret.openai_key',
        'secret.google_api_key',
        'secret.github_token',
        'secret.aws_access_key',
        'secret.slack_token',
        'secret.named_assignment',
    )


def test_a_redactor_is_a_redactor():
    from syndica.kernel.redaction import Redactor

    assert isinstance(RuleRedactor(), Redactor)


# --- 格式规则 ---------------------------------------------------------------------------------

_FORMATS = [
    (_OPENROUTER, 'secret.openrouter_key'),
    (_OPENAI, 'secret.openai_key'),
    *[
        ('sk-' + kind + _run(_ALNUM, 48, 11), 'secret.openai_key')
        for kind in ('', 'svcacct-', 'admin-')
    ],
    ('sk-' + _run(_ALNUM, 40, 12), 'secret.openai_key'),
    (_GOOGLE, 'secret.google_api_key'),
    *[
        ('gh' + kind + '_' + _run(_ALNUM, 36, 13), 'secret.github_token')
        for kind in ('p', 'o', 'u', 's', 'r')
    ],
    (_GITHUB_PAT, 'secret.github_token'),
    (_AWS, 'secret.aws_access_key'),
    ('AS' + 'IA' + _run(_UPPER32, 16, 14), 'secret.aws_access_key'),
    *[
        ('xo' + 'x' + kind + '-' + '123456789012-' + _run(_ALNUM, 24, 15), 'secret.slack_token')
        for kind in ('a', 'b', 'p', 'r', 's')
    ],
    *[
        ('xo' + 'xe.xo' + 'x' + kind + '-1-' + _run(_ALNUM, 24, 16), 'secret.slack_token')
        for kind in ('b', 'p')
    ],
    ('xa' + 'pp-1-' + _run(_ALNUM, 32, 16), 'secret.slack_token'),
]


@pytest.mark.parametrize(('secret', 'rule_id'), _FORMATS)
def test_each_format_rule_replaces_its_whole_public_format(secret: str, rule_id: str):
    assert _redactor().redact(f'key: {secret} (end)') == f'key: {_placeholder(rule_id)} (end)'


def test_keys_of_different_formats_are_all_replaced_whatever_their_order():
    text = f'{_SLACK} {_GITHUB} {_OPENROUTER}'

    assert _redactor().redact(text) == ' '.join(
        _placeholder(rule)
        for rule in ('secret.slack_token', 'secret.github_token', 'secret.openrouter_key')
    )


@pytest.mark.parametrize(
    'context',
    ['\x1b[1;32m{}', 'ok\\n{}', 'x\\t{}', 'x\\r{}', 'cb?x=1%26token%3D{}', '{}done'],
    ids=['colour', 'escaped-newline', 'escaped-tab', 'escaped-return', 'url-encoded', 'word-after'],
)
def test_a_format_is_found_right_after_a_colour_code_an_escape_or_url_encoding(context: str):
    """`\\b` 在 `m`、`n`、`D` 与 key 之间认不出开头：格式规则在抹掉这些之后的副本上找。"""
    redacted = _redactor().redact(context.format(_GITHUB))

    assert redacted == context.format(_placeholder('secret.github_token'))


@pytest.mark.parametrize(
    ('secret', 'rule_id'),
    [pair for pair in _FORMATS if pair[1] not in {'secret.openai_key', 'secret.slack_token'}],
)
def test_a_fixed_length_format_is_replaced_even_when_a_word_follows(secret: str, rule_id: str):
    assert _redactor().redact(f'{secret}done') == f'{_placeholder(rule_id)}done'


def test_the_commit_gate_finds_a_key_inside_a_json_escaped_log_line():
    line = '+' + json.dumps({'log': 'ok\n' + _GITHUB})

    assert _redactor().scan(line) == (Finding(1, 'secret.github_token'),)


@pytest.mark.parametrize(
    'lookalike',
    [
        'sk-or-v1-' + _run(_HEX, 63, 1),
        'sk-or-v1-',
        'sk-' + _run(_ALNUM, 39, 11),
        'sk-spinner-wandering-cubes',
        '.sk-folding-cube-animation-delay-for-the-third-cube-in-grid',
        'sk-ThisIsALongCamelCaseIdentifierWithoutAnyDigits',
        'AI' + 'za' + _run(_ALNUM, 34, 3),
        'gh' + 'p_' + _run(_ALNUM, 35, 4),
        'AK' + 'IA' + _run('abcdefghijklmnop', 16, 6),
        'xo' + 'xb-123',
        'task-UpdateUserProfile2024ForTheNewDashboardFeatureX',
    ],
)
def test_a_format_rule_does_not_fire_on_a_lookalike(lookalike: str):
    """只有前缀、差一位、字符集不对、只有小写与连字符的长词都不算。"""
    assert _redactor().redact(f'see {lookalike} here') == f'see {lookalike} here'


# --- 私钥块 -----------------------------------------------------------------------------------


def test_a_private_key_block_is_replaced_whole_even_when_truncated():
    complete = f'before\n{_BEGIN}\n{_KEY_BODY}\n{_END}\nafter'
    truncated = f'before\n{_BEGIN}\n{_KEY_BODY}'
    public = '-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----'

    redactor = _redactor()

    assert redactor.redact(complete) == f'before\n{_placeholder("secret.private_key_block")}\nafter'
    assert redactor.redact(truncated) == f'before\n{_placeholder("secret.private_key_block")}'
    assert redactor.redact(public) == public


@pytest.mark.parametrize('complete', [True, False], ids=['with-end-line', 'truncated'])
def test_a_secret_inside_a_key_block_does_not_split_the_block(complete: bool):
    """块里的一行先被换成占位符时，按占位符切段会把块切开，余下的密钥行原样留下。"""
    first, *rest = _KEY_BODY.split('\n')
    block = f'{_BEGIN}\n{first}\n' + '\n'.join(rest) + (f'\n{_END}' if complete else '')

    redacted = _redactor(first).redact(f'x\n{block}\ny')

    assert redacted == f'x\n{_placeholder("secret.private_key_block")}' + (
        '\ny' if complete else ''
    )
    assert all(line not in redacted for line in rest)


def test_a_truncated_key_written_inside_a_json_string_is_replaced():
    """JSON 把换行写成 `\\n` 两个字符：按真换行认的块规则在原文里看不到一行行的密钥。"""
    text = json.dumps({'log': f'cat id\n{_BEGIN}\n{_KEY_BODY}'})

    redacted = _redactor().redact(text)

    assert redacted == f'{{"log": "cat id\\n{_placeholder("secret.private_key_block")}'


def test_the_private_lines_of_a_putty_key_stay_whole_around_a_known_secret():
    first, *rest = _KEY_BODY.split('\n')
    head = 'PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 3\n'

    redacted = _redactor(first).redact(f'{head}{_KEY_BODY}\nPrivate-MAC: ab')

    assert redacted == f'{head}{_placeholder("secret.private_key_block")}\nPrivate-MAC: ab'


def test_a_mention_of_a_begin_line_without_key_material_is_left_alone():
    text = f'Look for `{_BEGIN}` in the file, then run the checker.\nNext step.'

    assert _redactor().redact(text) == text


def test_key_material_after_a_truncated_begin_line_starts_at_sixteen_characters():
    line = _run(_ALNUM + '+/', 16, 37)
    redactor = _redactor()

    assert redactor.redact(f'{_BEGIN}\n{line}') == _placeholder('secret.private_key_block')
    assert redactor.redact(f'{_BEGIN}\n{line[:15]}') == f'{_BEGIN}\n{line[:15]}'


def test_an_end_line_with_another_label_does_not_close_the_block():
    other_end = '-----END ' + 'RSA PRIVATE' + ' KEY-----'
    text = f'{_BEGIN}\n{_KEY_BODY}\n{other_end}\ntrailing {_KEY_BODY[:30]}'

    assert _redactor().redact(text) == _placeholder('secret.private_key_block')


@pytest.mark.parametrize(
    ('begin', 'end'),
    [
        ('-----BEGIN PGP ' + 'PRIVATE KEY BLOCK-----', '-----END PGP ' + 'PRIVATE KEY BLOCK-----'),
        (
            '---- BEGIN SSH2 ENCRYPTED ' + 'PRIVATE KEY ----',
            '---- END SSH2 ENCRYPTED ' + 'PRIVATE KEY ----',
        ),
    ],
    ids=['pgp', 'rfc4716'],
)
def test_pgp_and_rfc4716_private_keys_are_replaced(begin: str, end: str):
    text = f'x\n{begin}\n\n{_KEY_BODY}\n{end}\ny'

    assert _redactor().redact(text) == f'x\n{_placeholder("secret.private_key_block")}\ny'


def test_the_private_lines_of_a_putty_key_are_replaced():
    head = 'PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 3\n'

    redacted = _redactor().redact(f'{head}{_KEY_BODY}\nPrivate-MAC: ab')

    assert redacted == f'{head}{_placeholder("secret.private_key_block")}\nPrivate-MAC: ab'


def test_a_truncated_encrypted_key_with_header_lines_is_replaced_to_the_end():
    begin = '-----BEGIN ' + 'RSA PRIVATE' + ' KEY-----'
    headers = 'Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B2C3D4E5F\n\n'

    assert _redactor().redact(f'x\n{begin}\n{headers}{_KEY_BODY}') == (
        f'x\n{_placeholder("secret.private_key_block")}'
    )


# --- Authorization 与 URL -------------------------------------------------------------------


@pytest.mark.parametrize(
    'template',
    [
        'curl -H "Authorization: Bearer {}" https://x',
        'curl -H "Authorization:Bearer {}" https://x',
        "{{'Authorization': 'Bearer {}'}}",
        '{{"Authorization": "token {}"}}',
        'Authorization: "Bearer {}"',
        'Authorization: Bearer "{}"',
        'send: GET / HTTP/1.1\\r\\nAuthorization: Basic {}\\r\\n',
        'HTTP_AUTHORIZATION=Bearer {}',
        'Proxy-Authorization: Basic {}',
        'authorization: bearer {}',
    ],
)
def test_the_credential_after_an_authorization_scheme_is_replaced(template: str):
    credential = _run(_ALNUM, 24, 12)

    redacted = _redactor().redact(template.format(credential))

    assert redacted == template.format(_placeholder('secret.authorization_header'))


def test_an_authorization_credential_ends_at_an_escaped_line_break():
    text = 'send: GET / HTTP/1.1\\r\\nAuthorization: Basic Ab3defgh\\r\\nHost: x'

    assert _redactor().redact(text) == (
        'send: GET / HTTP/1.1\\r\\nAuthorization: Basic '
        f'{_placeholder("secret.authorization_header")}\\r\\nHost: x'
    )


@pytest.mark.parametrize(
    'text',
    [
        'Authorization: required for every request',
        'Authorization: token required',
        'Authorization: Basic auth is not supported',
        'Authorization: Bearer Ab3defg',
        'Authorization: tokenization-v2 is described below',
    ],
)
def test_an_authorization_line_without_a_credential_is_left_alone(text: str):
    """凭证要够 8 个字符，且有数字、大写或 `+/=._-`。"""
    assert _redactor().redact(text) == text


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('git remote add origin https://bob:' + 'hunter2@host/r.git', 'https://{}@host/r.git'),
        ('redis://:' + _run(_ALNUM, 24, 20) + '@cache:6379/0', 'redis://{}@cache:6379/0'),
        ('https://u:p@ss' + 'word@host/x', 'https://{}@host/x'),
        ('https://u:pa:ss' + 'word@host/x', 'https://{}@host/x'),
        ('{"url": "https:\\/\\/u:' + 'secret1@host\\/x"}', '{{"url": "https:\\/\\/{}@host\\/x"}}'),
    ],
)
def test_the_userinfo_of_a_url_is_replaced_and_the_rest_kept(text: str, expected: str):
    redacted = _redactor().redact(text)

    assert redacted.endswith(expected.format(_placeholder('secret.url_userinfo')))


def test_the_userinfo_of_a_url_on_an_added_diff_line_is_replaced():
    text = '+https://bob:' + _run(_ALNUM, 12, 84) + '@host/r.git'

    assert _redactor().redact(text) == f'+https://{_placeholder("secret.url_userinfo")}@host/r.git'


@pytest.mark.parametrize(
    'text',
    ['https://example.com:8080/a@b', 'git@github.com:org/repo.git', 'ssh://git@host/repo'],
)
def test_a_url_without_a_password_keeps_its_userinfo(text: str):
    assert _redactor().redact(text) == text


# --- 逐字换掉的密钥值 --------------------------------------------------------------------------


def test_a_secret_value_is_replaced_verbatim():
    value = _run(_ALNUM, 12, 13)

    redacted = _redactor(value).redact(f'connect with {value} now')

    assert redacted == f'connect with {_placeholder("secret.env_value")} now'


def test_longer_values_are_replaced_before_the_shorter_ones_they_contain():
    """短值是长值的一段时先换短的，长值剩下的部分就留在了文字里。"""
    short = _run(_ALNUM, 10, 14)
    long = short + '-extra-part'

    redacted = _redactor(short, long).redact(f'x {long} y')

    assert redacted == f'x {_placeholder("secret.env_value")} y'


def test_a_whole_token_is_replaced_by_one_placeholder_before_fragments_are_looked_at():
    redacted = _redactor(_OPENROUTER).redact(f'<{_OPENROUTER}>')

    assert redacted == f'<{_placeholder("secret.env_value")}>'


def test_a_secret_that_starts_with_a_slash_is_still_replaced_and_gated():
    value = '/' + _run(_ALNUM + '+', 39, 21)
    redactor = _redactor(value)

    assert redactor.redact(f'k={value}') == f'k={_placeholder("secret.env_value")}'
    assert redactor.scan(f'k={value}') == (Finding(1, 'secret.env_value'),)


def test_a_secret_that_is_a_substring_of_a_placeholder_does_not_break_placeholders():
    """逐字值只在还没被换掉的段里找：否则一个恰好是 `secret.env_value` 的值会把占位符拆坏。"""
    redactor = _redactor(_run(_ALNUM, 24, 50), 'secret.env_value')

    redacted = redactor.redact(f'a {_run(_ALNUM, 24, 50)} b')

    assert redacted == f'a {_placeholder("secret.env_value")} b'


@pytest.mark.parametrize('bad', ['', 7, None], ids=['empty', 'int', 'none'])
def test_a_secret_must_be_a_non_empty_string(bad: object):
    with pytest.raises(ConfigError) as caught:
        RuleRedactor(secrets=['ok-value-123', bad])  # pyright: ignore[reportArgumentType]

    assert 'secrets[1]' in caught.value.received
    assert '空串' in caught.value.next_action


def test_a_rejected_secret_is_not_echoed_in_the_error():
    value = _run(_ALNUM, 24, 51)

    with pytest.raises(ConfigError) as caught:
        RuleRedactor(secrets=[value, ''])

    assert value not in str(caught.value)


@pytest.mark.parametrize(
    'secrets',
    [
        'ok-value-1234567',
        b'ok-value-1234567',
        {'API_KEY': 'ok-value-1234567'},
        types.MappingProxyType({'API_KEY': 'ok-value-1234567'}),
        None,
        7,
    ],
    ids=['str', 'bytes', 'mapping', 'environ-like', 'none', 'int'],
)
def test_secrets_must_be_a_collection_of_values(secrets: object):
    """一个字符串会被逐字符拆开（每个字母都成了密钥），一个映射只交出键（变量名，值反而漏掉）。"""
    with pytest.raises(ConfigError) as caught:
        RuleRedactor(secrets=secrets)  # pyright: ignore[reportArgumentType]

    assert type(secrets).__name__ in caught.value.received
    assert '逐字符' in caught.value.next_action
    assert 'ok-value' not in str(caught.value)


def test_secrets_may_come_from_any_iterable_of_values():
    value = _run(_ALNUM, 24, 85)

    for secrets in (iter([value]), {'K': value}.values(), {value}):
        assert RuleRedactor(secrets=secrets).redact(value) == _placeholder('secret.env_value')


@pytest.mark.parametrize('method', ['redact', 'scan'])
@pytest.mark.parametrize('text', [b'bytes', None, 7], ids=['bytes', 'none', 'int'])
def test_redact_and_scan_take_text_only(method: str, text: object):
    with pytest.raises(ContractViolation) as caught:
        getattr(_redactor(), method)(text)

    assert type(text).__name__ in caught.value.received
    assert '解码' in caught.value.next_action


# --- 令牌片段 ---------------------------------------------------------------------------------


def test_a_truncated_token_is_recognised_by_any_fragment_of_sixteen_characters():
    fragment = _OPENROUTER[9 : 9 + 30]

    redacted = _redactor(_OPENROUTER).redact(f'key sk-or-v1-{fragment}...')

    assert redacted == f'key sk-or-v1-{_placeholder("secret.env_value")}...'


def test_the_last_sixteen_characters_of_a_token_are_a_fragment_too():
    redactor = _redactor(_OPENROUTER)

    assert redactor.redact(f'[{_OPENROUTER[-16:]}]') == f'[{_placeholder("secret.env_value")}]'


def test_the_fragment_threshold_is_exactly_sixteen_characters():
    core = _OPENROUTER[9:]
    redactor = _redactor(_OPENROUTER)

    assert redactor.redact(f'[{core[10:26]}]') == f'[{_placeholder("secret.env_value")}]'
    assert redactor.redact(f'[{core[10:25]}]') == f'[{core[10:25]}]'


@pytest.mark.parametrize(
    'prefix',
    [
        'sk-or-v1-',
        'sk-proj-',
        'sk-svcacct-',
        'sk-admin-',
        'sk-',
        'github' + '_pat_',
        *['gh' + kind + '_' for kind in 'pousr'],
        *['xo' + 'x' + kind + '-' for kind in 'abprs'],
        'AI' + 'za',
        'AK' + 'IA',
        'AS' + 'IA',
    ],
)
def test_the_public_prefix_does_not_count_toward_a_fragment(prefix: str):
    """前缀加 15 个字符凑够了 16 个，但前缀是公开的，只露出 15 个字符不算泄露。"""
    value = prefix + _run(_UPPER32, 40, 31)
    text = f'keys look like {value[: len(prefix) + 15]}...'

    assert _redactor(value).redact(text) == text


def test_overlapping_occurrences_of_one_fragment_are_all_found():
    """同一个 16 字符窗口在文字里首尾相叠地出现两次时，第二次也要找到：它盖住的后半段没有别的
    窗口。"""
    period = 'Ab3dEf9h'
    secret = period * 2 + 'Zq9Lm4Xw'

    assert _redactor(secret).redact(f'[{period * 3}]') == f'[{_placeholder("secret.env_value")}]'


def test_overlapping_occurrences_of_a_secret_value_are_all_found():
    period = 'Ab3d'

    assert (
        _redactor(period * 3).redact(f'<{period * 4}>') == f'<{_placeholder("secret.env_value")}>'
    )


def test_a_value_becomes_a_token_at_twenty_characters():
    token = _run(_ALNUM, 20, 18)
    short = _run(_ALNUM, 19, 19)

    assert _redactor(token).redact(f'<{token[2:18]}>') == f'<{_placeholder("secret.env_value")}>'
    assert _redactor(short).redact(f'<{short[2:18]}>') == f'<{short[2:18]}>'


def test_a_base64_secret_with_a_slash_is_still_matched_by_fragments():
    value = _run(_ALNUM, 12, 26) + '/' + _run(_ALNUM + '+', 27, 27)

    redacted = _redactor(value).redact(f's: {value[:30]}')

    assert redacted == f's: {_placeholder("secret.env_value")}'


@pytest.mark.parametrize(
    ('value', 'text'),
    [
        ('config/keys/service-account-prod.json', 'see config/keys/service-account-dev.json'),
        ('correct horse battery staple', 'the classic correct horse battery example'),
        (
            'redis://:' + _run(_ALNUM, 24, 32) + '@cache:6379/0',
            'redis://:' + _run(_ALNUM, 24, 32)[:7] + ' is how it starts',
        ),
    ],
)
def test_a_path_a_passphrase_or_a_url_is_only_replaced_verbatim(value: str, text: str):
    """相对路径、口令与 URL 里的一段是普通文字，按片段换会把正文里相似的路径、句子与 URL 开头
    一起换掉。"""
    redactor = _redactor(value)

    assert redactor.redact(text) == text
    assert redactor.redact(value) == _placeholder('secret.env_value')


def test_ordinary_words_are_untouched_by_fragment_matching():
    redactor = _redactor(_OPENROUTER, _GITHUB, _AWS)
    text = 'class EventListener:\n    def addEventListener(self, handler): ...\n'

    assert redactor.redact(text) == text


# --- 占位符不透明，redact 幂等 ----------------------------------------------------------------


def test_a_token_in_the_user_slot_of_a_url_keeps_its_own_placeholder():
    """M2 第二遍会让 `secret.url_userinfo` 吃掉令牌的占位符（它带着冒号）：不幂等。"""
    redactor = _redactor()
    once = redactor.redact(f'https://{_GITHUB}@github.com/r')

    assert once == f'https://{_placeholder("secret.github_token")}@github.com/r'
    assert redactor.redact(once) == once


@pytest.mark.parametrize(
    ('template', 'rule_id'),
    [
        ('https://u:{}@host/x', 'secret.url_userinfo'),
        ('https://{}:hunter2more@minio.local/x', 'secret.url_userinfo'),
        ('redis://:{}tail0more@h/0', 'secret.url_userinfo'),
        ('Authorization: Bearer {}', 'secret.authorization_header'),
        ('Authorization: Bearer {}.tail0more', 'secret.authorization_header'),
    ],
    ids=['url-password', 'url-user', 'url-tail', 'authorization', 'authorization-tail'],
)
def test_a_context_rule_covers_the_whole_credential_even_around_a_known_secret(
    template: str, rule_id: str
):
    """一个已知密钥值嵌在凭证里时，上下文规则照样看到整段凭证：先换掉那一段再找，凭证剩下的部分就
    原样留下了。"""
    value = _run(_ALNUM, 24, 52)
    text = template.format(value)

    redacted = _redactor(value).redact(text)

    assert _placeholder(rule_id) in redacted
    assert value not in redacted
    for leftover in ('hunter2more', 'tail0more'):
        assert leftover not in redacted


def _jwt(seed: int, claims: str) -> str:
    header = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
    payload = 'eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6' + claims
    return f'{header}.{payload}.{_run(_ALNUM + "_-", 43, seed)}'


def test_a_sibling_token_under_authorization_is_hidden_whole_when_another_is_known():
    """宿主环境里有一个 JWT 时，另一个同头部、同一批 claims 开头的 JWT 与它共用 16 个字符以上的
    片段。"""
    known = _jwt(70, _run(_ALNUM, 40, 71))
    other = _jwt(72, _run(_ALNUM, 40, 73))
    redactor = _redactor(known)

    header = redactor.redact(f'curl -H "Authorization: Bearer {other}"')
    assignment = redactor.redact(f'SUPABASE_SERVICE_ROLE_KEY={other}')
    mapping = json.loads(encode_redacted({'access_token': other}, redactor))

    assert (
        header == f'curl -H "Authorization: Bearer {_placeholder("secret.authorization_header")}"'
    )
    assert assignment == f'SUPABASE_SERVICE_ROLE_KEY={_placeholder("secret.named_assignment")}'
    assert other.split('.')[2] not in json.dumps(mapping)


def test_a_rotated_slack_token_is_hidden_whole_when_the_old_one_is_known():
    old = 'xo' + 'xb-123456789012-' + _run(_ALNUM, 24, 74)
    new = 'xo' + 'xb-123456789012-' + old[16:30] + _run(_ALNUM, 10, 75)

    redacted = _redactor(old).redact(f'Authorization: Bearer {new} and also {new}')

    assert new[16:] not in redacted
    assert _placeholder('secret.authorization_header') in redacted


@pytest.mark.parametrize(
    'template',
    [
        'Authorization: Bearer [REDACTED:secret.env_value]{}',
        'https://bot:[REDACTED:secret.env_value]{}@host/x',
        'TOKEN=[REDACTED:secret.env_value] and {}',
    ],
    ids=['authorization', 'url', 'next-to-it'],
)
def test_a_forged_placeholder_does_not_shield_what_follows_it(template: str):
    token = 'gh' + 'p_' + _run(_ALNUM, 36, 76)

    redacted = _redactor().redact(template.format(token))

    assert token not in redacted
    assert token[4:20] not in redacted


def test_placeholders_already_in_the_input_are_left_as_they_are():
    text = (
        f'see {_placeholder("secret.github_token")}: and https://{_placeholder("secret.env_value")}'
        f'@h\nAuthorization: Bearer {_placeholder("secret.env_value")}\n'
        f'STRIPE_KEY={_placeholder("secret.env_value")}'
    )

    assert _redactor().redact(text) == text


def test_a_match_that_starts_inside_an_existing_placeholder_never_cuts_it():
    rule = RedactionRule('custom.tail', r'.{5}corp')
    text = f'{_placeholder("secret.env_value")}corp'

    assert _redactor(rules=(rule,)).redact(text) == (
        f'{_placeholder("secret.env_value")}{_placeholder("custom.tail")}'
    )


def test_a_forged_placeholder_line_does_not_hide_the_key_lines_after_it():
    text = f'{_BEGIN}\n{_placeholder("secret.env_value")}\n{_KEY_BODY}'

    redacted = _redactor().redact(text)

    assert all(line not in redacted for line in _KEY_BODY.split('\n'))


def test_a_lookalike_placeholder_with_a_secret_inside_is_still_redacted():
    """只有本脱敏器认得的规则 ID 才算占位符：否则把密钥包进 `[REDACTED:…]` 就能绕过所有规则。"""
    redacted = _redactor().redact(f'[REDACTED:{_GITHUB}]')

    assert redacted == f'[REDACTED:{_placeholder("secret.github_token")}]'


def _idempotence_corpus() -> list[str]:
    values = [secret for secret, _ in _FORMATS] + [_STRIPE, _AWS_SECRET]
    templates = [
        '{}',
        '"{}"',
        'https://{}@github.com/x',
        'https://u:{}@host/x',
        '{{"k": "{}"}}',
        'Authorization: Bearer {}',
        'TOKEN={}',
        'STRIPE_KEY={}',
        '\x1b[1;32m{}',
        'ok\\n{}',
        '{}done',
    ]
    corpus = [template.format(value) for value in values for template in templates]
    corpus += [json.dumps({'log': f'x\n{value}'}) for value in values]
    corpus += [f'{_BEGIN}\n{_KEY_BODY}\n{_END}', f'{_BEGIN}\n{_KEY_BODY}', 'corp-abc.internal']
    corpus += [
        f'{_BEGIN}\n{_GITHUB}\nmore',
        f'{_BEGIN}\n{_OPENAI}\n{_KEY_BODY}',
        _AWS + _OPENAI,
        _GITHUB + _AWS + _SLACK,
        f'{_AWS_SECRET[:24]}{_GITHUB}',
        f'STRIPE_KEY={_AWS_SECRET[:24]}{_STRIPE}',
    ]
    return corpus


@pytest.mark.parametrize(
    'text',
    [
        _AWS + _OPENAI,
        _GITHUB + _AWS + _SLACK,
        f'{_AWS_SECRET[:24]}{_GITHUB}',
        _GITHUB * 6,
        'OPENAI_KEY_' + _OPENAI,
    ],
    ids=['two-glued', 'three-glued', 'after-a-secret-value', 'six-glued', 'after-underscore'],
)
def test_glued_keys_are_all_replaced_in_one_pass(text: str):
    """紧挨着的两个 key 之间没有分隔：第一遍只换掉第一个、第二遍才认出下一个，就既漏又不幂等。"""
    redactor = _redactor(_AWS_SECRET[:24])

    once = redactor.redact(text)

    assert all(token not in once for token in (_AWS, _OPENAI, _GITHUB, _SLACK, _AWS_SECRET[:24]))
    assert redactor.redact(once) == once


@pytest.mark.parametrize(
    ('pattern', 'text'),
    [
        (r'TICKET-\d+$', 'TICKET-1TICKET-2'),
        (r'corp\d\b', 'corp7corp8 and corp9'),
        (r'\Abeta', 'alphabeta'),
        (r'\(corp\)', f'TOKEN={_STRIPE}(corp)'),
        (r'zq', f'zq{_OPENAI}'),
    ],
    ids=['end-anchor', 'word-boundary', 'start-anchor', 'after-an-assignment', 'glued-before-key'],
)
def test_user_rules_with_anchors_stay_idempotent_and_hide_their_neighbours(pattern: str, text: str):
    rules = (RedactionRule('custom.first', pattern), RedactionRule('custom.second', 'alpha'))
    redactor = _redactor(rules=rules)

    once = redactor.redact(text)

    assert redactor.redact(once) == once
    assert _OPENAI not in once


def test_redacting_twice_is_the_same_as_redacting_once():
    rules = (RedactionRule('custom.internal_host', r'corp-[a-z0-9]+\.internal'),)
    redactor = _redactor(_AWS_SECRET[:24], _OPENROUTER, rules=rules)

    for text in _idempotence_corpus():
        once = redactor.redact(text)
        assert redactor.redact(once) == once, text


# --- 用户规则 ---------------------------------------------------------------------------------


def test_a_user_rule_replaces_its_matches_with_its_own_placeholder():
    rule = RedactionRule('custom.internal_host', r'corp-[a-z0-9]+\.internal')

    redacted = _redactor(rules=(rule,)).redact('reach corp-db7.internal now')

    assert redacted == f'reach {_placeholder("custom.internal_host")} now'


def test_user_rules_come_after_every_built_in_rule():
    """用户规则排在内置之前时，一条宽的用户规则会抢走内置规则的命中，占位符说的就不是那种密钥了。"""
    broad = RedactionRule('custom.anything_long', r'[A-Za-z0-9_]{30,}')

    redacted = _redactor(rules=(broad,)).redact(f'x {_GITHUB} y')

    assert redacted == f'x {_placeholder("secret.github_token")} y'


def test_overlapping_matches_become_one_placeholder_named_after_the_longest():
    first = RedactionRule('custom.first', r'alpha-\d+')
    second = RedactionRule('custom.second', r'alpha-\d+ beta')

    redacted = _redactor(rules=(first, second)).redact('alpha-1 beta and alpha-2')

    assert redacted == f'{_placeholder("custom.second")} and {_placeholder("custom.first")}'


def test_equally_long_matches_take_the_rule_listed_first():
    first = RedactionRule('custom.first', r'corp-[a-z]+')
    second = RedactionRule('custom.second', r'corp-[a-z]{2,}')

    assert _redactor(rules=(first, second)).redact('x corp-db y') == (
        f'x {_placeholder("custom.first")} y'
    )


def test_no_rule_can_match_the_characters_of_an_existing_placeholder():
    rule = RedactionRule('custom.after', r'\] beta')
    text = f'{_placeholder("custom.after")} beta'

    assert _redactor(rules=(rule,)).redact(text) == text


def test_a_user_rule_that_only_matches_zero_width_inserts_nothing():
    """`(?=corp)` 匹配不了空串，加载时拦不住；它在文本里的零宽命中不能每次插进一个占位符。"""
    redactor = _redactor(rules=(RedactionRule('custom.lookahead', r'(?=corp)'),))

    once = redactor.redact('corp-db7 and corp-db8')

    assert once == 'corp-db7 and corp-db8'
    assert redactor.redact(once) == once


@pytest.mark.parametrize(
    ('rule_id', 'pattern', 'expected'),
    [
        ('internal_host', r'corp', '以 custom. 开头'),
        ('custom.', r'corp', '以 custom. 开头'),
        ('custom.has space', r'corp', '以 custom. 开头'),
        ('custom.bad]', r'corp', '以 custom. 开头'),
        ('custom.-x', r'corp', '以 custom. 开头'),
        ('custom..x', r'corp', '以 custom. 开头'),
        ('secret.github_token', r'corp', '不与内置规则重名'),
        ('custom.bad_regex', r'corp-(', '合法的 Python 正则'),
        ('custom.empty', r'[a-z]*', '不能匹配空串'),
        ('custom.empty_alternative', r'corp|', '不能匹配空串'),
        ('custom.blank', '', '非空的正则字符串'),
    ],
)
def test_a_malformed_user_rule_is_refused_with_why(rule_id: str, pattern: str, expected: str):
    with pytest.raises(ConfigError) as caught:
        RedactionRule(rule_id, pattern)

    assert expected in caught.value.expected
    assert caught.value.next_action


@pytest.mark.parametrize(
    'pattern',
    [r'REDACTED', r'\[REDACTED:secret\.\w+\]', r'custom\.mine', r'env_value'],
)
def test_a_user_rule_that_matches_a_placeholder_is_refused(pattern: str):
    with pytest.raises(ConfigError) as caught:
        RuleRedactor(rules=[RedactionRule('custom.mine', pattern)])

    assert '不匹配任何占位符' in caught.value.expected
    assert '[REDACTED:' in caught.value.received


def test_the_placeholder_a_rule_would_match_is_named_in_rule_order():
    rules = [RedactionRule('custom.zz', 'corp'), RedactionRule('custom.aa', r'REDACTED:custom')]

    with pytest.raises(ConfigError) as caught:
        RuleRedactor(rules=rules)

    assert "'[REDACTED:custom.zz]'" in caught.value.received


def test_two_user_rules_with_the_same_id_are_refused():
    rules = [RedactionRule('custom.host', 'corp-a'), RedactionRule('custom.host', 'corp-b')]

    with pytest.raises(ConfigError) as caught:
        RuleRedactor(rules=rules)

    assert "'custom.host'" in caught.value.received


@pytest.mark.parametrize(
    'rules',
    [
        'custom.host',
        [('custom.host', 'corp')],
        (rule for rule in [RedactionRule('custom.host', 'corp')]),
    ],
    ids=['a-string', 'tuples-not-rules', 'generator'],
)
def test_rules_must_be_a_sequence_of_redaction_rules(rules: object):
    with pytest.raises(ConfigError):
        RuleRedactor(rules=rules)  # pyright: ignore[reportArgumentType]


def test_a_user_rule_id_is_at_most_one_hundred_and_seven_characters():
    assert RedactionRule('custom.' + 'x' * 100, 'corp').rule_id.endswith('x')
    with pytest.raises(ConfigError):
        RedactionRule('custom.' + 'x' * 101, 'corp')


def test_a_user_rule_id_must_be_a_string():
    with pytest.raises(ConfigError) as caught:
        RedactionRule(7, 'corp')  # pyright: ignore[reportArgumentType]

    assert '是字符串' in caught.value.expected


# --- secret.named_assignment --------------------------------------------------------------------


@pytest.mark.parametrize(
    'template',
    [
        'STRIPE_KEY={}',
        'export AWS_SECRET_ACCESS_KEY={}',
        '{{"apiKey": "{}"}}',
        "{{'client_secret': '{}'}}",
        'password: {}',
        'db_password = "{}"',
        '--api-key={}',
        'PGPASSWORD="{}"',
        'apiKey := "{}"',
        "'api_key' => '{}'",
        '$apiKey = "{}";',
        'os.environ["API_KEY"] = "{}"',
        'docker run --env=API_KEY={} img',
        'api-key-prod={}',
        'stripe.secret.live: {}',
        "curl -b 'sessionid={}'",
    ],
)
@pytest.mark.parametrize('value', [_STRIPE, _AWS_SECRET], ids=['stripe', 'aws-secret'])
def test_a_credential_assigned_to_a_secret_name_is_replaced(template: str, value: str):
    redacted = _redactor().redact(template.format(value))

    assert redacted == template.format(_placeholder('secret.named_assignment'))


@pytest.mark.parametrize(
    'text',
    [
        "key='llm_retry_attempts'",
        'key: str',
        'api_key=api_key',
        'token=self._token_value_here',
        'TOKEN_URL=https://example.com/oauth/token',
        'KEY_PATH=config/keys/service-account-prod2.json',
        'PIN_TOKEN=0123456789-_.~+/=',
        'PIN_TOKEN=0123456789-_~+=9',
        'version_key=1.2.3-alpha.4567890',
        'API_KEY=your-api-key-goes-here-12345',
        'API_KEY=EXAMPLEKEY1234567890ABCD',
        'MY_TOKEN=${TOKEN}',
        'secret = os.environ["SECRET_VALUE_12345"]',
        f'NOT_A_NAME={_STRIPE}',
        f'API_KEY={_STRIPE}(',
        f'API_KEY={_STRIPE}[0]',
        'TOKEN=abc123',
    ],
)
def test_an_assignment_that_is_not_a_credential_is_left_alone(text: str):
    assert _redactor().redact(text) == text


def _mix(singles: int, doubles: int, triples: int) -> str:
    """固定字符频数的串：熵只取决于频数。字母数字都有，不是十六进制，不像示例。"""
    pool = 'aZ3kQ9mX1bW7pL5nV2cR8tY4dS6fH0gJ'
    counts = [1] * singles + [2] * doubles + [3] * triples
    rounds = [
        [pool[index] for index, count in enumerate(counts) if count > round_] for round_ in range(3)
    ]
    return ''.join(char for round_ in rounds for char in round_)


def test_the_entropy_floor_is_three_and_a_half_bits_per_character():
    below, above = _mix(0, 6, 5), _mix(0, 7, 5)
    assert 3.4 < _entropy(below) < 3.45 and 3.55 < _entropy(above) < 3.56

    assert _redactor().redact(f'API_KEY={below}') == f'API_KEY={below}'
    assert (
        _redactor().redact(f'API_KEY={above}')
        == f'API_KEY={_placeholder("secret.named_assignment")}'
    )


def _hex_mix(counts: list[int]) -> str:
    pool = '7f3a0c9e1b5d2864'
    rounds = [
        [pool[index] for index, count in enumerate(counts) if count > round_] for round_ in range(4)
    ]
    return ''.join(char for round_ in rounds for char in round_)


def test_a_long_hex_key_has_a_lower_entropy_floor():
    """十六进制的每字符熵至多 4 比特：随机的 32 位十六进制 key 常在 3.5 以下。"""
    long_key = _hex_mix([3] * 10 + [2])
    short_key = _hex_mix([2] * 10)
    assert len(long_key) == 32 and 3.0 <= _entropy(long_key) < 3.5
    assert len(short_key) == 20 and 3.0 <= _entropy(short_key) < 3.5

    assert _redactor().redact(f'ENCRYPTION_KEY={long_key}') == (
        f'ENCRYPTION_KEY={_placeholder("secret.named_assignment")}'
    )
    assert _redactor().redact(f'ENCRYPTION_KEY={short_key}') == f'ENCRYPTION_KEY={short_key}'


def test_a_long_credential_is_replaced_whole():
    """`AWS_SESSION_TOKEN` 与长 JWT 有上千个字符。"""
    token = _run(_ALNUM + '+/', 1200, 79)

    assert _redactor().redact(f'AWS_SESSION_TOKEN={token}') == (
        f'AWS_SESSION_TOKEN={_placeholder("secret.named_assignment")}'
    )


def test_a_quoted_password_may_contain_symbols():
    password = 'P@ss' + _run(_ALNUM, 14, 80) + '!#%'

    assert _redactor().redact(f'db_password = "{password}"') == (
        f'db_password = "{_placeholder("secret.named_assignment")}"'
    )


@pytest.mark.parametrize(
    'text',
    [
        f'- name: STRIPE_KEY\n  value: {_STRIPE}',
        f'{{"name": "STRIPE_KEY", "value": "{_STRIPE}"}}',
    ],
    ids=['yaml', 'json'],
)
def test_an_environment_table_entry_is_judged_by_its_name(text: str):
    redacted = _redactor().redact(text)

    assert _STRIPE not in redacted
    assert _placeholder('secret.named_assignment') in redacted


def test_an_environment_table_entry_with_an_ordinary_name_is_left_alone():
    text = f'- name: RELEASE_CHANNEL\n  value: {_STRIPE}'

    assert _redactor().redact(text) == text


@pytest.mark.parametrize(
    'text',
    [
        f'Authorization: {_STRIPE}',
        f'HTTP_AUTHORIZATION={_AWS_SECRET}',
        'Authorization: Bot ' + _run(_ALNUM, 32, 81),
    ],
    ids=['no-scheme', 'no-scheme-env', 'bot'],
)
def test_an_authorization_credential_without_a_known_scheme_is_replaced(text: str):
    redacted = _redactor().redact(text)

    assert redacted.endswith(_placeholder('secret.authorization_header'))


def test_a_schemeless_authorization_value_that_is_not_a_credential_is_left_alone():
    text = 'Authorization: required_for_every_private_endpoint'

    assert _redactor().redact(text) == text


@pytest.mark.parametrize(
    'word',
    ['example', 'placeholder', 'xxxx', 'changeme', 'your', 'dummy'],
)
def test_a_value_that_says_it_is_an_example_is_left_alone(word: str):
    value = f'{word}{_run(_ALNUM, 18, 82)}'

    assert _redactor().redact(f'API_KEY={value}') == f'API_KEY={value}'


@pytest.mark.parametrize(
    'text',
    [
        'key: npm-cache-v2-linux-x64-node20',
        'SSH_KEY=id_ed25519_github.pub',
        'KEY_FILE=service-account-prod-2024.json',
        'secret_key = settings.SECRET_KEY_FROM_ENV_2',
        'integrity_key: sha384-oqVuAfXRKap7fdgcCY5uykM6R9GqQ8Kuxy9rx7HNQlGYl1kPzQho1wx4JwY8wC',
        'SECRET_NAME=projects/my-project/secrets/db-password/versions/3',
        'KEY_DIR=etc/ssl.d/Private2Keys',
    ],
    ids=['slug', 'key-file', 'json-file', 'attribute', 'sri-hash', 'resource-path', 'path'],
)
def test_a_name_that_looks_secret_with_a_value_that_names_something_is_left_alone(text: str):
    assert _redactor().redact(text) == text


def test_named_assignments_are_not_part_of_the_commit_gate():
    assert _redactor().scan(f'STRIPE_KEY={_STRIPE}') == ()


def _scanned_for_named_assignments() -> list[Path]:
    roots = [
        _ROOT / 'src' / 'syndica',
        _ROOT / 'tests' / 'scenarios',
        _ROOT / 'tests' / 'kernel' / 'data',
        *sorted(_ROOT.glob('tests/**/fixtures')),
    ]
    return sorted(
        path
        for root in roots
        for path in root.rglob('*')
        if path.is_file() and '__pycache__' not in path.parts
    )


def test_named_assignment_has_no_hit_in_the_source_tree_or_the_test_fixtures():
    """误报门槛：只看名字与长度时，`key='llm_retry_attempts'` 这类写法在源码里有几十处。"""
    redactor = _redactor()
    hits = []
    for path in _scanned_for_named_assignments():
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        if _placeholder('secret.named_assignment') in redactor.redact(text):
            hits.append(str(path.relative_to(_ROOT)))

    assert hits == []


def _walked(root: Path) -> list[Path]:
    return sorted(
        Path(directory) / name
        for directory, _, files in os.walk(root)
        if '__pycache__' not in Path(directory).parts
        for name in files
    )


@pytest.mark.parametrize(
    'anchor',
    ['src/syndica/kernel/retry.py', 'tests/fixtures/redaction/near_misses.fixture'],
)
def test_the_false_positive_gate_reads_files_full_of_secret_named_assignments(anchor: str):
    """门槛扫的范围要证明自己没缩：它一定读过满是「名字像密钥」的赋值的文件——其中的值要先过得了
    赋值的形状，才真的考到了「值像凭证」的判断。"""
    path = _ROOT / anchor
    shaped = [
        match
        for match in _ASSIGNMENT.finditer(path.read_text(encoding='utf-8'))
        if looks_secret(match.group('name') or match.group('quoted_name'))
    ]

    assert path in _scanned_for_named_assignments()
    assert len(shaped) >= (2 if anchor.startswith('src') else 12)


def test_the_false_positive_gate_covers_the_source_tree_and_every_fixture_directory():
    scanned = _scanned_for_named_assignments()
    roots = [
        _ROOT / 'src' / 'syndica',
        _ROOT / 'tests' / 'scenarios',
        _ROOT / 'tests' / 'kernel' / 'data',
        *sorted(_ROOT.glob('tests/**/fixtures')),
    ]

    assert len(roots) >= 5
    for root in roots:
        assert [path for path in scanned if root in path.parents] == _walked(root)


# --- encode_redacted --------------------------------------------------------------------------


def test_encoding_redacts_every_leaf_before_it_encodes():
    """先编码再脱敏会漏：转义后的私钥块认不出、带引号的值多了反斜杠、`url_userinfo` 跨过字符串
    边界。"""
    quoted_value = 'pä"ss' + _run(_ALNUM, 16, 53)
    url = 'https://bob:' + _run(_ALNUM, 12, 54) + '@host/x'
    block = f'{_BEGIN}\n{_KEY_BODY}\n{_END}'
    payload = {'outer': [{'key_block': block}, {'url': url}], 'env': ['x', quoted_value]}

    encoded = encode_redacted(payload, _redactor(quoted_value))
    decoded = json.loads(encoded)

    assert isinstance(encoded, RedactedText)
    assert decoded['outer'][0]['key_block'] == _placeholder('secret.private_key_block')
    assert decoded['outer'][1]['url'] == f'https://{_placeholder("secret.url_userinfo")}@host/x'
    assert decoded['env'][1] == _placeholder('secret.env_value')
    for leaked in (quoted_value, _KEY_BODY.split('\n')[1], url.split('@')[0][12:]):
        assert leaked not in encoded


def test_a_credential_under_a_secret_key_is_replaced_by_the_mapping_rule():
    encoded = encode_redacted(
        {'env': {'STRIPE_KEY': _STRIPE, 'EDITOR': 'vim'}, 'ok': 'STRIPE_KEY'}, _redactor()
    )

    assert json.loads(encoded) == {
        'env': {'EDITOR': 'vim', 'STRIPE_KEY': _placeholder('secret.named_assignment')},
        'ok': 'STRIPE_KEY',
    }


@pytest.mark.parametrize(
    'payload',
    [
        {'headers': {'Authorization': 'Bearer {}'}},
        {'headers': {'proxy-authorization': 'Basic {}'}},
        {'headers': [['Authorization', 'Bearer {}']]},
    ],
    ids=['mapping', 'proxy-lowercase', 'pairs'],
)
def test_an_authorization_header_in_a_mapping_is_redacted_like_the_header(payload: object):
    token = _run(_ALNUM, 40, 83)
    filled = json.loads(json.dumps(payload).replace('{}', token))

    encoded = encode_redacted(filled, _redactor())

    assert token not in encoded
    assert _placeholder('secret.authorization_header') in encoded


def test_a_sentence_under_a_secret_key_is_not_taken_for_a_credential():
    payload = {'password_hint': 'Ab3dEf9hIj2kLm5nOp7q is not the password'}

    assert json.loads(encode_redacted(payload, _redactor())) == payload


def test_every_credential_in_a_list_under_a_secret_key_is_replaced():
    decoded = json.loads(
        encode_redacted({'API_KEYS': [_STRIPE, 'short', _AWS_SECRET]}, _redactor())
    )

    placeholder = _placeholder('secret.named_assignment')
    assert decoded == {'API_KEYS': [placeholder, 'short', placeholder]}


@pytest.mark.parametrize('first', [_STRIPE, 'primary'], ids=['two-keys', 'label-then-key'])
def test_both_items_of_a_two_item_list_under_a_secret_key_are_judged(first: str):
    """两项的字符串列表也按一对键值读：第一项不能因为被当成了键名就逃过判断，第二项也不能因为
    「键名」不像密钥就丢掉外层的判断。"""
    decoded = json.loads(encode_redacted({'api_keys': [first, _AWS_SECRET]}, _redactor()))

    placeholder = _placeholder('secret.named_assignment')
    assert decoded == {'api_keys': [placeholder if first == _STRIPE else first, placeholder]}


def test_a_credential_anywhere_under_a_secret_key_is_replaced():
    payload = {
        'STRIPE_KEY': {'value': _STRIPE},
        'credentials': {'prod': [{'id': _AWS_SECRET}], 'note': 'rotate monthly'},
        'config': {'value': _STRIPE},
    }

    decoded = json.loads(encode_redacted(payload, _redactor()))

    placeholder = _placeholder('secret.named_assignment')
    assert decoded == {
        'STRIPE_KEY': {'value': placeholder},
        'config': {'value': _STRIPE},
        'credentials': {'note': 'rotate monthly', 'prod': [{'id': placeholder}]},
    }


def test_values_that_are_not_strings_under_a_secret_key_are_kept():
    payload = {'max_tokens': 100, 'token_ttl': 1.5, 'auth': True, 'api_key': None, 'keys': []}

    assert json.loads(encode_redacted(payload, _redactor())) == payload


def test_the_encoding_is_one_line_of_utf8_whatever_the_strings_hold():
    """trace 按行存：`str.splitlines()` 在 U+2028、U+2029、U+0085 处断行，落单的代理项写不进
    UTF-8 文件，C1 控制字符到了终端是控制序列。"""
    value = {'s\u2028k': 'a\u2028b\u2029c\x85d\x9be\x7ff\ud800g\nh'}

    encoded = encode_redacted(value, _redactor())

    assert encoded.splitlines() == [encoded]
    assert encoded.encode('utf-8').decode('utf-8') == encoded
    assert [char for char in encoded if '\x7f' <= char <= '\x9f'] == []
    assert json.loads(encoded) == value
    assert isinstance(encoded, RedactedText)


@pytest.mark.parametrize('redactor', [None, object(), 'redactor'], ids=['none', 'object', 'str'])
def test_encoding_needs_a_redactor_even_without_strings(redactor: object):
    with pytest.raises(ContractViolation) as caught:
        encode_redacted({'n': 1}, redactor)  # pyright: ignore[reportArgumentType]

    assert type(redactor).__name__ in caught.value.received
    assert 'RuleRedactor' in caught.value.next_action


def test_an_environment_table_in_a_mapping_is_judged_by_the_name_field():
    payload = {
        'env': [{'name': 'STRIPE_KEY', 'value': _STRIPE}, {'name': 'MODE', 'value': _STRIPE}]
    }

    decoded = json.loads(encode_redacted(payload, _redactor()))

    assert decoded['env'][0]['value'] == _placeholder('secret.named_assignment')
    assert decoded['env'][1]['value'] == _STRIPE


def test_a_key_is_judged_by_its_own_name_not_by_a_secret_inside_it():
    """键里嵌着一个令牌时，脱敏后的键名带着 `[REDACTED:secret.…]`，按它判就会把每个值都当成密钥。"""
    key = 'note_' + _GITHUB

    decoded = json.loads(encode_redacted({key: _STRIPE}, _redactor()))

    assert decoded == {f'note_{_placeholder("secret.github_token")}': _STRIPE}


def test_a_structure_nested_too_deep_is_refused_with_why():
    deep: object = 'leaf'
    for _ in range(5_000):
        deep = [deep]

    with pytest.raises(ContractViolation) as caught:
        encode_redacted(deep, _redactor())

    assert '递归上限' in caught.value.expected


def test_an_integer_json_cannot_print_is_refused_with_the_fix():
    with pytest.raises(ContractViolation) as caught:
        encode_redacted({'n': 10**5_000}, _redactor())

    assert '过大的整数' in caught.value.next_action


def test_mapping_keys_are_redacted_and_colliding_keys_are_all_kept_in_key_order():
    tokens = sorted('gh' + 'p_' + _run(_ALNUM, 36, seed) for seed in (55, 56, 57))
    forwards = dict(zip(tokens, (1, 2, 3), strict=True))
    backwards = dict(reversed(list(forwards.items())))

    encoded = encode_redacted(forwards, _redactor())

    placeholder = _placeholder('secret.github_token')
    assert json.loads(encoded) == {placeholder: 1, f'{placeholder}#2': 2, f'{placeholder}#3': 3}
    assert encode_redacted(backwards, _redactor()) == encoded


def test_encoding_is_canonical_json():
    encoded = encode_redacted({'b': [1, 2.5, None, True], 'a': 'ä', 'c': ('x',)}, _redactor())

    assert encoded == '{"a":"ä","b":[1,2.5,null,true],"c":["x"]}'


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ({1: 'x'}, '映射键都是字符串'),
        ({'k': {1, 2}}, '只收 JSON 能表达的值'),
        ({'k': b'bytes'}, '只收 JSON 能表达的值'),
        ({'k': float('nan')}, '有限数'),
    ],
    ids=['int-key', 'set', 'bytes', 'nan'],
)
def test_encoding_refuses_what_json_cannot_say(value: object, expected: str):
    with pytest.raises(ContractViolation) as caught:
        encode_redacted(value, _redactor())

    assert expected in caught.value.expected


def test_an_error_path_shows_keys_only_after_redaction():
    with pytest.raises(ContractViolation) as caught:
        encode_redacted({_GITHUB: {'inner': object()}}, _redactor())

    assert _GITHUB not in str(caught.value)
    assert _placeholder('secret.github_token') in caught.value.received


def test_editing_redacted_text_gives_back_a_plain_string():
    encoded = encode_redacted({'a': 1}, _redactor())

    assert type(encoded + 'x') is str
    assert type(encoded[1:]) is str


@pytest.mark.parametrize(
    'build',
    [
        lambda encoded: RedactedText('{"a":1}'),
        lambda encoded: type(encoded)('{"a":1}'),
        lambda encoded: encoded.__class__('{"a":1}'),
        lambda encoded: RedactedText('{"a":1}', _mint=object()),
    ],
    ids=['constructor', 'type-of', 'class-attribute', 'forged-token'],
)
def test_redacted_text_is_built_only_by_the_encoder(build: Any):
    encoded = encode_redacted({'a': 1}, _redactor())

    with pytest.raises(ContractViolation) as caught:
        build(encoded)

    assert 'encode_redacted' in caught.value.next_action


def test_copying_redacted_text_keeps_it_and_pickling_gives_back_plain_text():
    encoded = encode_redacted({'a': 1}, _redactor())

    assert copy.copy(encoded) is encoded
    assert copy.deepcopy({'k': [encoded]})['k'][0] is encoded
    restored = pickle.loads(pickle.dumps(encoded))
    assert (restored, type(restored)) == (encoded, str)


def _short_writes(monkeypatch: pytest.MonkeyPatch, limit: int) -> list[int]:
    """让 `os.write` 每次至多写进 `limit` 个字节：文件大小上限、磁盘将满时就是这样，不报错。"""
    real = os.write
    sizes: list[int] = []

    def short(fd: int, data: Any) -> int:
        sizes.append(len(data))
        return real(fd, bytes(data)[:limit]) if limit else 0

    monkeypatch.setattr('syndica.kernel.redaction.os.write', short)
    return sizes


def test_write_all_finishes_a_short_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sizes = _short_writes(monkeypatch, 3)
    path = tmp_path / 'out'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT)
    try:
        write_all(descriptor, b'0123456789')
    finally:
        os.close(descriptor)

    assert path.read_bytes() == b'0123456789'
    assert sizes == [10, 7, 4, 1]


def test_write_all_refuses_a_write_that_makes_no_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _short_writes(monkeypatch, 0)
    descriptor = os.open(tmp_path / 'out', os.O_WRONLY | os.O_CREAT)
    try:
        with pytest.raises(OSError, match='腾出磁盘空间'):
            write_all(descriptor, b'x')
    finally:
        os.close(descriptor)


def test_write_redacted_writes_one_whole_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _short_writes(monkeypatch, 2)
    encoded = encode_redacted({'s': 'ä\u2028b'}, _redactor())
    path = tmp_path / 'out.jsonl'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT)
    try:
        write_redacted(descriptor, encoded, newline=True)
        write_redacted(descriptor, encoded)
    finally:
        os.close(descriptor)

    assert path.read_text(encoding='utf-8') == f'{encoded}\n{encoded}'


@pytest.mark.parametrize('text', ['{"a":1}', b'{"a":1}', None], ids=['str', 'bytes', 'none'])
def test_write_redacted_checks_the_type_at_runtime(tmp_path: Path, text: object):
    """静态类型挡不住 `Any`：从 JSON 里读出来的值就是 `Any`。"""
    path = tmp_path / 'out'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT)
    try:
        with pytest.raises(ContractViolation) as caught:
            write_redacted(descriptor, cast(Any, text))
    finally:
        os.close(descriptor)

    assert 'encode_redacted' in caught.value.next_action
    assert path.read_bytes() == b''


# --- 规则集摘要与版本 -------------------------------------------------------------------------


def test_the_digest_covers_user_rules_but_not_secret_values():
    plain = _redactor()
    with_secret = _redactor(_run(_ALNUM, 24, 56))
    with_rule = _redactor(rules=(RedactionRule('custom.host', 'corp-[a-z]+'),))
    other_rule = _redactor(rules=(RedactionRule('custom.host', 'corp-[a-z0-9]+'),))

    assert plain.digest == with_secret.digest
    assert len({plain.digest, with_rule.digest, other_rule.digest}) == 3
    assert with_rule.rules == (RedactionRule('custom.host', 'corp-[a-z]+'),)


def test_the_digest_of_a_ruleset_is_stable_across_releases():
    """回放拿记录下的摘要与自己的比：同一套规则在两个版本里算出不同的摘要，就是一次假的分叉。"""
    ruleset = _redactor(rules=(RedactionRule('custom.hete', 'corp-é[a-z]+'),))
    expected = hashlib.sha256(
        json.dumps(
            {
                'builtin': list(RULE_IDS),
                'custom': [['custom.hete', 'corp-é[a-z]+']],
                'rules_version': RULES_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()

    assert ruleset.digest == expected


# 黄金语料的摘要，按 RULES_VERSION 记。内置规则或 `encode_redacted` 的行为一变，摘要就变：升
# RULES_VERSION，把新摘要记在新版本下。只改了语料（加样例）而规则没变时，换掉当前版本的摘要、不升
# 版本。
_GOLDEN = {1: 'a3fbd53e1ede0eb782e1975f9a9afdb0642b97d8cd61e874d34a9ad25911ebef'}


def _golden_outputs() -> list[str]:
    """语料自成一份、不借别的用例的样例：加强别的用例不该逼着换摘要。"""
    token = _run(_ALNUM, 40, 62)
    value = _run(_ALNUM, 24, 60)
    redactor = _redactor(value, token)
    texts = [
        *(f'key: {secret} (end)' for secret, _ in _FORMATS),
        f'{_BEGIN}\n{_KEY_BODY}\n{_END}',
        f'{_BEGIN}\n{_KEY_BODY}',
        json.dumps({'log': f'cat id\n{_BEGIN}\n{_KEY_BODY}'}),
        'PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 3\n' + _KEY_BODY,
        f'connect {value} now',
        f'key {token[5:35]}...',
        'Authorization: Basic ' + _run(_ALNUM, 24, 61),
        f'Authorization: {_STRIPE}',
        'git remote add origin https://bob:' + 'hunter2@host/r.git',
        f'https://{value}:tail0more@h/x',
        f'https://{_GITHUB}@github.com/r',
        f'{_AWS}{_OPENAI}',
        f'{{"apiKey": "{_STRIPE}"}}',
        f'- name: STRIPE_KEY\n  value: {_STRIPE}',
        f'docker run --env=API_KEY={_AWS_SECRET} img',
        'version_key=1.2.3-alpha.4567890',
        "key='llm_retry_attempts'",
    ]
    structures: list[object] = [
        {'env': {'STRIPE_KEY': _STRIPE, 'EDITOR': 'vim'}, 'headers': [['Authorization', token]]},
        {'credentials': {'prod': [{'id': _AWS_SECRET}]}, 'env': [{'name': 'K', 'value': _STRIPE}]},
        {_GITHUB: 1, 'gh' + 'p_' + _run(_ALNUM, 36, 63): 2, 'line\u2028': 'a\x85b', 'n': 2.5},
    ]
    return [redactor.redact(text) for text in texts] + [
        encode_redacted(structure, redactor) for structure in structures
    ]


def test_the_golden_corpus_exercises_every_built_in_rule():
    outputs = '\n'.join(_golden_outputs())

    assert [rule for rule in RULE_IDS if _placeholder(rule) not in outputs] == []


def test_the_rules_version_moves_with_the_built_in_rules():
    digest = hashlib.sha256('\0'.join(_golden_outputs()).encode('utf-8')).hexdigest()

    assert RULES_VERSION in _GOLDEN, (
        f'RULES_VERSION={RULES_VERSION} 没有记下黄金语料的摘要：把 {digest} 记进 _GOLDEN'
    )
    assert _GOLDEN[RULES_VERSION] == digest, (
        '内置规则的输出变了：升 RULES_VERSION 并把新摘要记在新版本下；'
        '只改了语料就换掉当前版本的摘要'
    )


# --- looks_secret -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'name',
    [
        'API_KEY',
        'PGPASSWORD',
        'NGROK_AUTHTOKEN',
        'VOLC_SECRETKEY',
        'BW_SESSION',
        'AZURE_STORAGE_CONNECTION_STRING',
        'SSH_AUTH_SOCK',
        'apiKey',
        'client-secret',
    ],
)
def test_secret_names_are_recognised(name: str):
    assert looks_secret(name)


@pytest.mark.parametrize('name', ['PWD', 'OLDPWD', 'GIT_AUTHOR_NAME', 'EDITOR', 'PATH', 'KEYBOARD'])
def test_ordinary_names_are_not_secret(name: str):
    assert not looks_secret(name)


# --- 提交闸 -----------------------------------------------------------------------------------


def test_the_commit_gate_reports_lines_and_rules_but_not_the_secret():
    value = _run(_ALNUM, 20, 17)
    text = f'line one\ntoken = "{value}"\nkey = "{_GITHUB}"\n{_BEGIN}\n{_KEY_BODY}\n{_END}\n'

    findings = _redactor(value).scan(text)

    assert findings == (
        Finding(2, 'secret.env_value'),
        Finding(3, 'secret.github_token'),
        Finding(4, 'secret.private_key_block'),
    )
    assert [field.name for field in dataclasses.fields(Finding)] == ['line', 'rule_id']


def test_the_commit_gate_counts_lines_by_newline_only():
    text = f'a\x0cb\rc\u2028d\nkey = "{_GITHUB}"'

    assert _redactor().scan(text) == (Finding(2, 'secret.github_token'),)


def test_the_commit_gate_does_no_fragment_matching():
    """片段匹配允许误报，拦提交不允许。"""
    truncated = 'sk-or-v1-' + _OPENROUTER[9 : 9 + 30]

    assert _redactor(_OPENROUTER).scan(f'key = "{truncated}"') == ()


def test_the_commit_gate_ignores_user_rules():
    rule = RedactionRule('custom.internal_host', r'corp-[a-z0-9]+\.internal')

    assert _redactor(rules=(rule,)).scan('host = corp-db7.internal') == ()


@pytest.mark.parametrize(
    ('value', 'blocked'),
    [
        (_run('abcdefgh23456789', 16, 33), True),
        (_run('abcdefgh23456789', 15, 33), False),
        (_run('abcdefghijklmnop', 16, 34), False),
        ('correct-horse-staple', True),
    ],
    ids=['sixteen-mixed', 'fifteen-mixed', 'sixteen-one-kind', 'letters-and-punctuation'],
)
def test_the_commit_gate_blocks_secret_values_of_sixteen_mixed_characters(
    value: str, blocked: bool
):
    findings = _redactor(value).scan(f'x = {value}')

    assert findings == ((Finding(1, 'secret.env_value'),) if blocked else ())


@pytest.mark.parametrize(
    ('begin', 'end'),
    [
        (_BEGIN, _END),
        (
            '---- BEGIN SSH2 ENCRYPTED ' + 'PRIVATE KEY ----',
            '---- END SSH2 ENCRYPTED ' + 'PRIVATE KEY ----',
        ),
    ],
    ids=['pem', 'rfc4716'],
)
def test_the_commit_gate_blocks_a_complete_key_with_a_line_of_forty_characters(
    begin: str, end: str
):
    redactor = _redactor()
    line = _run(_ALNUM + '+/', 40, 35)

    assert redactor.scan(f'a\n{begin}\n{line}\n{end}\n') == (
        Finding(2, 'secret.private_key_block'),
    )
    assert redactor.scan(f'a\n{begin}\n{line[:39]}\n{end}\n') == ()


def test_the_commit_gate_blocks_the_private_lines_of_a_putty_key():
    text = f'PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 3\n{_KEY_BODY}\nPrivate-MAC: ab'

    assert _redactor().scan(text) == (Finding(2, 'secret.private_key_block'),)
    assert _redactor().scan('Private-Lines: 1\nAAAAB3NzaC1\n') == ()


def test_the_commit_gate_reports_a_key_block_once_even_with_a_begin_line_inside():
    text = f'{_BEGIN}\n{_KEY_BODY}\n{_BEGIN}\n{_KEY_BODY}\n{_END}\n'

    assert _redactor().scan(text) == (Finding(1, 'secret.private_key_block'),)


def test_the_commit_gate_blocks_a_truncated_key_whose_first_line_is_a_whole_key_line():
    redactor = _redactor()
    line = _run(_ALNUM + '+/', 40, 38)

    assert redactor.scan(f'a\n{_BEGIN}\n{line}\n{line}') == (
        Finding(2, 'secret.private_key_block'),
    )
    assert redactor.scan(f'a\n{_BEGIN}\n{line[:39]}\n') == ()


def test_the_commit_gate_reports_findings_in_line_order():
    value = _run(_ALNUM, 20, 39)
    text = f'{_BEGIN}\n{_KEY_BODY}\n{_END}\ntoken = "{value}"'

    findings = _redactor(value).scan(text)

    assert findings == (Finding(1, 'secret.private_key_block'), Finding(6, 'secret.env_value'))


@pytest.mark.parametrize(
    'text',
    [
        'curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com',
        'git clone https://user:pass@example.com/repo.git',
        f'mention {_BEGIN} in the docs',
        f'{_BEGIN}\n...\n{_END}',
        'aws_access_key_id = ' + 'AK' + 'IA' + 'IOSFODNN7' + 'EXAMPLE',
        'OPENAI_API_KEY=' + 'sk-' + 'example' + _run(_ALNUM, 40, 36),
        'token = "' + 'gh' + 'p_' + 'x' * 36 + '"',
        'OPENAI_API_KEY=' + 'sk-' + 'x' * 48,
        'SLACK=' + 'xo' + 'xb-' + '0' * 11 + '-' + 'x' * 24,
    ],
)
def test_the_commit_gate_ignores_placeholders_and_documentation_examples(text: str):
    assert _redactor().scan(text) == ()


def test_a_key_with_six_distinct_characters_is_not_a_placeholder():
    text = 'token = "' + 'gh' + 'p_' + 'xy' * 18 + '"'

    assert _redactor().scan(text) == (Finding(1, 'secret.github_token'),)


def test_the_commit_gate_ignores_short_or_plain_secret_values():
    """宿主里 `POSTGRES_PASSWORD=postgres` 不该让每一行 `image: postgres` 都拦住提交。"""
    redactor = _redactor('postgres', 'disabled')

    assert redactor.scan('image: postgres:16\nif cache is disabled:') == ()
    assert redactor.redact('image: postgres:16') == f'image: {_placeholder("secret.env_value")}:16'


# --- 线性时间 ---------------------------------------------------------------------------------


def test_fragment_matching_is_linear_in_the_text():
    redactor = _redactor(_OPENROUTER, _GITHUB, _SLACK)
    text = ('ordinary output line with nothing secret in it\n' * 20_000) + _OPENROUTER[9:40]
    started = time.process_time()

    redacted = redactor.redact(text)

    assert time.process_time() - started < 2
    assert redacted.endswith(_placeholder('secret.env_value'))


@pytest.mark.parametrize(
    'text',
    [
        'a.' * 500_000,
        'sk-' * 200_000,
        f'{_BEGIN}\n' * 60_000,
        f'{_BEGIN}\n' * 60_000 + _END,
        ''.join(f'-----BEGIN L{n} PRIVATE KEY-----\n' for n in range(40_000)),
        _OPENROUTER[9:40] * 100_000,
        'a=' + 'b' * 1_000_000,
        'a_' * 500_000,
        '"k":' * 200_000,
        ('KEY=' + 'x' * 600 + ' ') * 2_000,
        ' a: ' * 250_000,
        _GITHUB * 20_000,
        ('Comment: ' + _BEGIN + '\n') * 8_000,
        'a=' * 1_500_000,
        ('Authorization: ' + 'x' * 30 + ' ') * 50_000,
    ],
    ids=[
        'scheme-like-run',
        'openai-prefix-run',
        'unfinished-keys',
        'begins-then-one-end',
        'distinct-labels',
        'fragments',
        'one-long-value',
        'one-long-name',
        'quoted-names',
        'over-long-values',
        'yaml-keys',
        'many-placeholders',
        'begin-lines-inside-headers',
        'name-equals-runs',
        'bare-authorization',
    ],
)
def test_redaction_and_the_commit_gate_are_linear(text: str):
    redactor = _redactor(_OPENROUTER)
    started = time.process_time()

    redactor.redact(text)
    redactor.scan(text)

    assert time.process_time() - started < 4


def test_digests_are_encoded_as_they_are_because_nothing_else_gets_in():
    digests = ['ab' * 32, '0f' * 32, 'ab' * 32]

    assert json.loads(encode_digests(digests)) == digests


@pytest.mark.parametrize(
    'bad',
    [['AB' * 32], ['ab' * 31], ['ab' * 33], [' ' + 'ab' * 32], ['api_key=' + 'a' * 56], [64]],
    ids=['uppercase', 'short', 'long', 'padded', 'text', 'int'],
)
def test_anything_but_a_digest_is_refused_by_the_unredacted_encoder(bad: list[Any]):
    with pytest.raises(ContractViolation) as caught:
        encode_digests(['cd' * 32, *bad])

    assert '第 1 项' in caught.value.received
    assert 'encode_redacted' in caught.value.next_action
    assert 'api_key' not in caught.value.received


def test_kernel_identifiers_are_kept_verbatim_even_under_a_key_named_like_a_secret():
    agent_id = 'c0ffee' + '0' * 26
    digest = 'ab' * 32
    redactor = RuleRedactor(rules=[RedactionRule('custom.hex', '[0-9a-f]{32}')])
    value = {'key': ['tool_call', agent_id, 1], 'note': f'id {agent_id}', 'digest': digest}

    kept = json.loads(encode_redacted(value, redactor, identifiers={agent_id, digest}))
    plain = json.loads(encode_redacted(value, redactor))

    assert kept == {
        'digest': digest,
        'key': ['tool_call', agent_id, 1],
        'note': 'id [REDACTED:custom.hex]',
    }
    assert plain['key'][1] != agent_id and plain['digest'] != digest


@pytest.mark.parametrize(
    'identifier', ['not-hex', 'AB' * 16, 'ab' * 15, 'ab' * 40, 'g' * 32, 'z9' * 32]
)
def test_only_kernel_shaped_identifiers_may_skip_redaction(identifier: str):
    with pytest.raises(ContractViolation) as caught:
        encode_redacted({'x': identifier}, RuleRedactor(), identifiers={identifier})

    assert '32 或 64 个小写十六进制' in caught.value.expected
    assert identifier not in str(caught.value)
    assert f'长度 {len(identifier)}' in caught.value.received


@pytest.mark.parametrize('identifier', ['0f' * 16, '0f' * 32])
def test_ids_and_digests_are_both_kernel_shaped(identifier: str):
    text = encode_redacted({'key': identifier}, RuleRedactor(), identifiers={identifier})

    assert json.loads(text) == {'key': identifier}


def test_a_prepared_structure_is_encoded_without_judging_its_leaves_by_outer_keys():
    looks_like_a_credential = 'aB3dE5fG7hJ9kL1mN2pQ4rS6'
    value = {'second-pass': {'id': looks_like_a_credential}, 'password': looks_like_a_credential}

    prepared = json.loads(encode_prepared(value, RuleRedactor()))
    judged = json.loads(encode_redacted(value, RuleRedactor()))

    assert prepared == value
    assert judged['password'] != looks_like_a_credential


@pytest.mark.parametrize('where', ['leaf', 'key'], ids=['leaf', 'key'])
def test_a_prepared_structure_refuses_a_string_that_was_never_redacted(where: str):
    token = 'ghp_' + ''.join('aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xYz1234'[:36])
    value = {'notes': [token]} if where == 'leaf' else {token: 1}

    with pytest.raises(ContractViolation) as caught:
        encode_prepared(value, RuleRedactor())

    assert '已经脱过敏' in caught.value.expected
    assert token not in str(caught.value)
    assert 'encode_redacted' in caught.value.next_action


def test_a_prepared_structure_keeps_kernel_identifiers_a_user_rule_would_change():
    agent_id = 'ab' * 16
    redactor = RuleRedactor(rules=[RedactionRule('custom.hex', '[0-9a-f]{32}')])

    text = encode_prepared({'key': ['tool_call', agent_id]}, redactor, identifiers={agent_id})

    assert json.loads(text) == {'key': ['tool_call', agent_id]}
    with pytest.raises(ContractViolation):
        encode_prepared({'key': ['tool_call', agent_id]}, redactor)
