"""最小脱敏器（spec D12）。

样例一律是运行时拼出的合成假值，不在源码里逐字出现：否则仓库自己的提交会撞上提交闸。
"""

from __future__ import annotations

import dataclasses
import json
import random
import time
from pathlib import Path

import pytest

from syndica.cli.redaction import RULE_IDS, Finding, Redactor

_ALNUM = 'aZ3kQ9mX1bW7pL5nV2cR8tY4dS6fH0gJ'
_HEX = '0123456789abcdef'


def _run(alphabet: str, length: int, seed: int) -> str:
    """定种子的伪随机串。按位置算出来的串是周期的：64 位的十六进制串里 16 字符的片段会重复出现，
    「最后一个片段」于是和前面某个片段是同一个。"""
    generator = random.Random(seed)
    return ''.join(generator.choice(alphabet) for _ in range(length))


_OPENROUTER = 'sk-or-v1-' + _run(_HEX, 64, 1)
_OPENAI = 'sk-' + 'proj-' + _run(_ALNUM, 48, 2)
_GOOGLE = 'AI' + 'za' + _run(_ALNUM + '_-', 35, 3)
_GITHUB = 'gh' + 'p_' + _run(_ALNUM, 36, 4)
_GITHUB_PAT = 'github' + '_pat_' + _run(_ALNUM + '_', 82, 5)
_AWS = 'AK' + 'IA' + _run('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567', 16, 6)
_SLACK = 'xo' + 'xb-' + '123456789012-' + _run(_ALNUM, 24, 7)
_BEGIN = '-----BEGIN ' + 'OPENSSH PRIVATE' + ' KEY-----'
_END = '-----END ' + 'OPENSSH PRIVATE' + ' KEY-----'
_KEY_BODY = '\n'.join(_run(_ALNUM + '+/', 64, seed) for seed in (8, 9, 10))


def _redactor(**environ: str) -> Redactor:
    return Redactor(environ)


def _placeholder(rule_id: str) -> str:
    return f'[REDACTED:{rule_id}]'


def test_the_rule_ids_are_exactly_the_ones_the_plan_names():
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
    )


# --- 格式规则 ---------------------------------------------------------------------------------

_UPPER32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
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


@pytest.mark.parametrize(
    'text',
    ['https://example.com:8080/a@b', 'git@github.com:org/repo.git', 'ssh://git@host/repo'],
)
def test_a_url_without_a_password_keeps_its_userinfo(text: str):
    assert _redactor().redact(text) == text


# --- 宿主环境里的值 ---------------------------------------------------------------------------


def test_a_secret_environment_value_is_replaced_verbatim():
    value = _run(_ALNUM, 12, 13)

    redacted = _redactor(DATABASE_PASSWORD=value).redact(f'connect with {value} now')

    assert redacted == f'connect with {_placeholder("secret.env_value")} now'


def test_longer_values_are_replaced_before_the_shorter_ones_they_contain():
    """短值是长值的一段时先换短的，长值剩下的部分就留在了文字里。"""
    short = _run(_ALNUM, 10, 14)
    long = short + '-extra-part'

    redacted = _redactor(SHORT_TOKEN=short, LONG_SECRET=long).redact(f'x {long} y')

    assert redacted == f'x {_placeholder("secret.env_value")} y'


def test_a_whole_token_is_replaced_by_one_placeholder_before_fragments_are_looked_at():
    redacted = _redactor(OPENROUTER_API_KEY=_OPENROUTER).redact(f'<{_OPENROUTER}>')

    assert redacted == f'<{_placeholder("secret.env_value")}>'


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('SERVICE_TOKEN_URL', 'https://example.com/oauth/token'),
        ('API_KEY', 'seven77'),
        ('EDITOR', 'vim-with-a-long-name'),
    ],
)
def test_values_that_do_not_look_like_credentials_are_left_alone(name: str, value: str):
    """不带凭证的 URL、短于 8 个字符的值、名字不像密钥的变量都不换。"""
    text = f'value is {value}'

    assert _redactor(**{name: value}).redact(text) == text


def test_an_existing_absolute_path_is_not_a_credential(tmp_path: Path):
    socket = tmp_path / 'agent-socket-for-this-session'
    socket.write_text('', encoding='utf-8')

    assert _redactor(SSH_AUTH_SOCK=str(socket)).redact(f'at {socket}') == f'at {socket}'


def test_a_secret_that_starts_with_a_slash_is_a_credential():
    """base64 的密钥 64 个里有一个以 `/` 开头：按「以 `/` 开头就是路径」判，它就永远不被替换。"""
    value = '/' + _run(_ALNUM + '+', 39, 21)
    redactor = _redactor(AWS_SECRET_ACCESS_KEY=value)

    assert redactor.redact(f'k={value}') == f'k={_placeholder("secret.env_value")}'
    assert redactor.scan(f'k={value}') == (Finding(1, 'secret.env_value'),)


def test_a_value_with_a_trailing_newline_is_replaced_without_it():
    token = _run(_ALNUM, 32, 22)
    redactor = _redactor(SERVICE_TOKEN=token + '\n')

    assert redactor.redact(f'"{token}"') == f'"{_placeholder("secret.env_value")}"'
    assert redactor.scan(f'"{token}"') == (Finding(1, 'secret.env_value'),)


def test_the_length_threshold_starts_at_eight_characters():
    value = _run(_ALNUM, 8, 15)

    assert _redactor(API_KEY=value).redact(value) == _placeholder('secret.env_value')


@pytest.mark.parametrize(
    'value',
    [
        'postgres://app:' + _run(_ALNUM, 12, 16) + '@db:5432/main',
        'redis://:' + _run(_ALNUM, 12, 30) + '@cache:6379/0',
        'https://' + _run(_ALNUM, 20, 23) + '@git.example.com/r.git',
        'http://[::1' + _run(_ALNUM, 12, 24),
    ],
    ids=['user-and-password', 'password-only', 'token-as-user', 'unparsable'],
)
def test_a_url_carrying_or_possibly_carrying_credentials_is_a_credential(value: str):
    redacted = _redactor(DB_AUTH=value).redact(f'DSN={value}')

    assert redacted == f'DSN={_placeholder("secret.env_value")}'


@pytest.mark.parametrize(
    'name',
    [
        'PGPASSWORD',
        'NGROK_AUTHTOKEN',
        'VOLC_SECRETKEY',
        'BW_SESSION',
        'AZURE_STORAGE_CONNECTION_STRING',
    ],
)
def test_compound_secret_names_are_recognised(name: str):
    value = _run(_ALNUM, 24, 25)

    assert _redactor(**{name: value}).redact(value) == _placeholder('secret.env_value')


# --- 令牌片段 ---------------------------------------------------------------------------------


def test_a_truncated_token_is_recognised_by_any_fragment_of_sixteen_characters():
    fragment = _OPENROUTER[9 : 9 + 30]

    redacted = _redactor(OPENROUTER_API_KEY=_OPENROUTER).redact(f'key sk-or-v1-{fragment}...')

    assert redacted == f'key sk-or-v1-{_placeholder("secret.env_value")}...'


def test_the_last_sixteen_characters_of_a_token_are_a_fragment_too():
    redactor = _redactor(OPENROUTER_API_KEY=_OPENROUTER)

    assert redactor.redact(f'[{_OPENROUTER[-16:]}]') == f'[{_placeholder("secret.env_value")}]'


def test_the_fragment_threshold_is_exactly_sixteen_characters():
    core = _OPENROUTER[9:]
    redactor = _redactor(OPENROUTER_API_KEY=_OPENROUTER)

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

    assert _redactor(SERVICE_API_KEY=value).redact(text) == text


def test_a_value_becomes_a_token_at_twenty_characters():
    token = _run(_ALNUM, 20, 18)
    short = _run(_ALNUM, 19, 19)

    assert _redactor(API_TOKEN=token).redact(f'<{token[2:18]}>') == (
        f'<{_placeholder("secret.env_value")}>'
    )
    assert _redactor(API_TOKEN=short).redact(f'<{short[2:18]}>') == f'<{short[2:18]}>'


def test_a_base64_secret_with_a_slash_is_still_matched_by_fragments():
    value = _run(_ALNUM, 12, 26) + '/' + _run(_ALNUM + '+', 27, 27)

    redacted = _redactor(AWS_SECRET_ACCESS_KEY=value).redact(f's: {value[:30]}')

    assert redacted == f's: {_placeholder("secret.env_value")}'


@pytest.mark.parametrize(
    ('name', 'value', 'text'),
    [
        (
            'KEY_PATH',
            'config/keys/service-account-prod.json',
            'see config/keys/service-account-dev.json',
        ),
        (
            'SIGNING_PASSWORD',
            'correct horse battery staple',
            'the classic correct horse battery example',
        ),
        (
            'REDIS_AUTH',
            'redis://:' + _run(_ALNUM, 24, 32) + '@cache:6379/0',
            'redis://:' + _run(_ALNUM, 24, 32)[:7] + ' is how it starts',
        ),
    ],
)
def test_a_path_a_passphrase_or_a_url_is_only_replaced_verbatim(name: str, value: str, text: str):
    """相对路径、口令与 URL 里的一段是普通文字，按片段换会把正文里相似的路径、句子与 URL 开头
    一起换掉。"""
    redactor = _redactor(**{name: value})

    assert redactor.redact(text) == text
    assert redactor.redact(value) == _placeholder('secret.env_value')


def test_ordinary_words_are_untouched_by_fragment_matching():
    redactor = _redactor(OPENROUTER_API_KEY=_OPENROUTER, GITHUB_TOKEN=_GITHUB, AWS_KEY=_AWS)
    text = 'class EventListener:\n    def addEventListener(self, handler): ...\n'

    assert redactor.redact(text) == text


# --- 提交闸 -----------------------------------------------------------------------------------


def test_the_commit_gate_reports_lines_and_rules_but_not_the_secret():
    value = _run(_ALNUM, 20, 17)
    text = f'line one\ntoken = "{value}"\nkey = "{_GITHUB}"\n{_BEGIN}\n{_KEY_BODY}\n{_END}\n'

    findings = _redactor(SERVICE_PASSWORD=value).scan(text)

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

    assert _redactor(OPENROUTER_API_KEY=_OPENROUTER).scan(f'key = "{truncated}"') == ()


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
def test_the_commit_gate_blocks_environment_values_of_sixteen_mixed_characters(
    value: str, blocked: bool
):
    findings = _redactor(SERVICE_PASSWORD=value).scan(f'x = {value}')

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

    findings = _redactor(SERVICE_PASSWORD=value).scan(text)

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


def test_the_commit_gate_ignores_short_or_plain_environment_values():
    """宿主里 `POSTGRES_PASSWORD=postgres` 不该让每一行 `image: postgres` 都拦住提交。"""
    redactor = _redactor(POSTGRES_PASSWORD='postgres', AUTH_MODE='disabled')

    assert redactor.scan('image: postgres:16\nif cache is disabled:') == ()
    assert redactor.redact('image: postgres:16') == f'image: {_placeholder("secret.env_value")}:16'


# --- 线性时间 ---------------------------------------------------------------------------------


def test_fragment_matching_is_linear_in_the_text():
    redactor = _redactor(OPENROUTER_API_KEY=_OPENROUTER, GITHUB_TOKEN=_GITHUB, SLACK_TOKEN=_SLACK)
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
    ],
    ids=[
        'scheme-like-run',
        'openai-prefix-run',
        'unfinished-keys',
        'begins-then-one-end',
        'distinct-labels',
        'fragments',
    ],
)
def test_redaction_and_the_commit_gate_are_linear(text: str):
    redactor = _redactor(OPENROUTER_API_KEY=_OPENROUTER)
    started = time.process_time()

    redactor.redact(text)
    redactor.scan(text)

    assert time.process_time() - started < 4
