"""宿主从自己的环境里挑出要逐字换掉的密钥值（M3 D4）。规则本身的用例在内核的测试里。

样例一律是运行时拼出的合成假值，不在源码里逐字出现：否则仓库自己的提交会撞上提交闸。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from syndica.cli.redaction import host_redactor, host_secrets
from syndica.kernel import Finding, RedactionRule

_ALNUM = 'aZ3kQ9mX1bW7pL5nV2cR8tY4dS6fH0gJ'


def _run(alphabet: str, length: int, seed: int) -> str:
    generator = random.Random(seed)
    return ''.join(generator.choice(alphabet) for _ in range(length))


def _placeholder(rule_id: str) -> str:
    return f'[REDACTED:{rule_id}]'


def test_a_secret_environment_value_is_replaced_verbatim():
    value = _run(_ALNUM, 12, 13)

    redacted = host_redactor({'DATABASE_PASSWORD': value}).redact(f'connect with {value} now')

    assert redacted == f'connect with {_placeholder("secret.env_value")} now'


def test_only_values_of_secret_names_are_taken_and_they_come_back_sorted():
    token, password = _run(_ALNUM, 24, 1), _run(_ALNUM, 12, 2)

    secrets = host_secrets(
        {'SERVICE_TOKEN': token, 'EDITOR': 'vim-with-a-long-name', 'DB_PASS': password}
    )

    assert secrets == tuple(sorted((token, password)))


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('SERVICE_TOKEN_URL', 'https://example.com/oauth/token'),
        ('API_KEY', 'seven77'),
        ('EDITOR', 'vim-with-a-long-name'),
        ('PWD', '/home/someone/project/path'),
    ],
)
def test_values_that_do_not_look_like_credentials_are_left_alone(name: str, value: str):
    """不带凭证的 URL、短于 8 个字符的值、名字不像密钥的变量都不换。"""
    assert host_secrets({name: value}) == ()


def test_an_existing_absolute_path_is_not_a_credential(tmp_path: Path):
    socket = tmp_path / 'agent-socket-for-this-session'
    socket.write_text('', encoding='utf-8')

    assert host_secrets({'SSH_AUTH_SOCK': str(socket)}) == ()


def test_a_value_that_happens_to_name_a_file_in_the_working_directory_is_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """只有绝对路径才可能是 `SSH_AUTH_SOCK` 这种指向磁盘的值；相对的名字按工作目录解析，宿主在哪个
    目录启动就会让哪些密钥悄悄不被替换。"""
    value = _run(_ALNUM, 16, 23)
    (tmp_path / value).write_text('', encoding='utf-8')
    monkeypatch.chdir(tmp_path)

    assert host_secrets({'SERVICE_TOKEN': value}) == (value,)


def test_a_secret_that_starts_with_a_slash_is_a_credential():
    """base64 的密钥 64 个里有一个以 `/` 开头：按「以 `/` 开头就是路径」判，它就永远不被替换。"""
    value = '/' + _run(_ALNUM + '+', 39, 21)
    redactor = host_redactor({'AWS_SECRET_ACCESS_KEY': value})

    assert redactor.redact(f'k={value}') == f'k={_placeholder("secret.env_value")}'
    assert redactor.scan(f'k={value}') == (Finding(1, 'secret.env_value'),)


def test_a_value_with_a_trailing_newline_is_taken_with_and_without_it():
    token = _run(_ALNUM, 32, 22)

    assert host_secrets({'SERVICE_TOKEN': token + '\n'}) == tuple(sorted((token, token + '\n')))
    assert host_redactor({'SERVICE_TOKEN': token + '\n'}).redact(f'"{token}"') == (
        f'"{_placeholder("secret.env_value")}"'
    )


def test_the_length_threshold_starts_at_eight_characters():
    assert host_secrets({'API_KEY': _run(_ALNUM, 8, 15)}) == (_run(_ALNUM, 8, 15),)
    assert host_secrets({'API_KEY': _run(_ALNUM, 7, 15)}) == ()


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
    assert host_secrets({'DB_AUTH': value}) == (value,)


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

    assert host_redactor({name: value}).redact(value) == _placeholder('secret.env_value')


def test_the_host_redactor_applies_the_user_rules_after_the_built_in_ones():
    rule = RedactionRule('custom.internal_host', r'corp-[a-z0-9]+\.internal')

    redacted = host_redactor({}, (rule,)).redact('ssh corp-db7.internal')

    assert redacted == f'ssh {_placeholder("custom.internal_host")}'
