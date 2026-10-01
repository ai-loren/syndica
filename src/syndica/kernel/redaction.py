"""脱敏（spec §12.3、M3 D4）：写进交接文件、trace 与导出给第三方的文本都先经这里。

规则各有稳定 ID，命中处换成 `[REDACTED:<规则 ID>]`，不留原文。`redact` 先让每条规则在同一份视图上
找出它要换的区间，再把相互重叠的区间并成一段、换成其中最长那一条规则的占位符：上下文规则
（`Authorization`、URL 里的 `user:pass@`、名字像密钥的赋值）看到的是完整的凭证，不会因为里面有一段
先被别的规则换掉而只换一半。已经找到的区间与输入里原有的占位符在视图里涂成不透明的空白，再找一轮，
直到找不出新的东西：第二次 `redact` 看到的就是第一次最后看到的那份视图，所以幂等，占位符也永远不会
被任何规则再次命中。

`scan` 是 git 收尾的提交闸：只报行号与规则 ID、不交回命中内容，不做片段匹配，也不跑
`secret.named_assignment` 与用户规则——这几类判断允许误报，拦提交不允许，一处误报就让每次提交都被拦。

先脱敏再截断：截到 72 个字符的提交标题会把一个完整格式的 key 截成认不出的一段。一段文字分几次交进来
（流式写入）时，调用方按行攒齐再交：跨两次调用的 key 哪一次都认不出。结构化的数据只经
`encode_redacted` 编码：先逐叶脱敏再编码，从不对编码之后的文本脱敏——JSON 转义之后带引号的值认不
出来，`secret.url_userinfo` 还会跨过字符串边界弄坏 JSON。

内核不读环境变量、不查磁盘：宿主环境里的密钥值由宿主挑好，经 `RuleRedactor(secrets=...)` 交进来。
"""

from __future__ import annotations

import bisect
import errno
import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, final, runtime_checkable

from syndica.kernel.errors import ConfigError, ContractViolation

__all__ = [
    'RULES_VERSION',
    'RULE_IDS',
    'Finding',
    'RedactedText',
    'RedactionRule',
    'Redactor',
    'RuleRedactor',
    'encode_redacted',
    'looks_secret',
    'write_all',
    'write_redacted',
]

RULE_IDS: tuple[str, ...] = (
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
# 内置规则或 `encode_redacted` 的行为一变（同样的输入得出不同的结果）就加一：回放靠它判断记录时
# 与回放时的规则是不是同一套，指纹按脱敏之后的内容算。
RULES_VERSION = 1

# 名字按 `_`、`.`、`-` 切开后，任一词（不分大小写）落在这里就像装着密钥。按词而不是按子串：
# `GIT_AUTHOR_NAME` 不是密钥，`SSH_AUTH_SOCK` 是。
_SECRET_WORDS = frozenset(
    {
        'AK',
        'APIKEY',
        'AUTH',
        'COOKIE',
        'CREDENTIAL',
        'CREDENTIALS',
        'DSN',
        'KEY',
        'KEYS',
        'PASS',
        'PASSPHRASE',
        'PASSWD',
        'PASSWORD',
        'PAT',
        'PW',
        'PWD',
        'SECRET',
        'SECRETS',
        'SESSION',
        'SESSIONID',
        'SK',
        'TOKEN',
        'TOKENS',
    }
)
# 连写的词（`PGPASSWORD`、`NGROK_AUTHTOKEN`、`VOLC_SECRETKEY`、`CLIENTSECRET`、`apiKey`）按词尾认。
_SECRET_SUFFIXES = ('KEY', 'PASSWORD', 'PASSWD', 'SECRET', 'TOKEN')
_SECRET_PAIRS = frozenset({('CONNECTION', 'STRING')})
# shell 自己的工作目录变量，不是密码。
_NOT_SECRET = frozenset({'PWD', 'OLDPWD'})
_NAME_PARTS = re.compile(r'[_.-]')

_MIN_TOKEN = 20
_MIN_FRAGMENT = 16
# 提交闸只按逐字值拦够长、够杂的值：宿主里 `POSTGRES_PASSWORD=postgres` 会让每一行 `image: postgres`
# 都拦住提交，而拦提交不允许误报。
_MIN_GATED_VALUE = 16
# 片段从公开前缀之后取：只露出 `sk-or-v1-` 不算泄露，算进去会让每一处提到前缀的文字都被换掉。
_PUBLIC_PREFIX = re.compile(
    r'\A(?:sk-or-v1-|sk-proj-|sk-svcacct-|sk-admin-|sk-|github_pat_|gh[pousr]_'
    r'|xox[abprs]-|AIza|AKIA|ASIA)'
)

# 格式规则在一份抹掉了颜色码、`\n` 这类转义与 `%XX` 的副本上找（等长换成空格，位置不变）：它们紧贴
# 在 key 前面时 `\b` 两边都是单词字符，认不出 key 的开头。各自完整的公开格式，只有前缀不算；不要求
# 结尾是词界，后面紧跟别的字符（`ghp_…done`）时前面那一段照样换掉。
_MASKED = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\\[nrt]|%[0-9A-Fa-f]{2}')
# 格式只要求前面不紧贴字母数字（不从一个词的中间起头）：`note_ghp_…` 这种拼在下划线后面的照样认。
_NOT_AFTER_ALNUM = r'(?<![A-Za-z0-9])'
_FORMAT_BODIES: tuple[tuple[str, str], ...] = (
    ('secret.openrouter_key', r'sk-or-v1-[0-9a-f]{64}'),
    (
        'secret.openai_key',
        # 真的 key 是随机串，有数字也有大写：只由小写与连字符组成的长词（CSS 类名）不是。前瞻限长，
        # 一长串 `sk-sk-sk-…` 才不会让每个起点都扫到末尾。
        r'sk-(?=[A-Za-z0-9_-]{0,200}\d)(?=[A-Za-z0-9_-]{0,200}[A-Z])[A-Za-z0-9_-]{40,}',
    ),
    ('secret.google_api_key', r'AIza[0-9A-Za-z_-]{35}'),
    ('secret.github_token', r'(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})'),
    ('secret.aws_access_key', r'(?:AKIA|ASIA)[A-Z0-9]{16}'),
    (
        'secret.slack_token',
        r'(?:xox[abprs]-\d{8,}-[A-Za-z0-9-]{16,}|xoxe\.xox[bp]-\d-[A-Za-z0-9-]{20,}'
        r'|xapp-\d-[A-Za-z0-9-]{20,})',
    ),
)
_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (rule_id, re.compile(_NOT_AFTER_ALNUM + body)) for rule_id, body in _FORMAT_BODIES
)
# 紧跟在上一个格式命中后面的格式：两段之间没有分隔，开头那条限制不成立。一条链在同一轮里找完，不必
# 每轮只多认出一个。
_GLUED = tuple((rule_id, re.compile(body)) for rule_id, body in _FORMAT_BODIES)
# 区间一样长时谁的名字留下：上下文规则（私钥块、`Authorization`、URL）盖住嵌在里面的逐字值，逐字值
# 盖住格式规则，用户规则排最后。
_PRECEDENCE = (
    'secret.private_key_block',
    'secret.authorization_header',
    'secret.url_userinfo',
    'secret.env_value',
    'secret.openrouter_key',
    'secret.openai_key',
    'secret.google_api_key',
    'secret.github_token',
    'secret.aws_access_key',
    'secret.slack_token',
    'secret.named_assignment',
)
# 私钥块：PEM（含 PGP 的 `… PRIVATE KEY BLOCK`）、RFC 4716 与 PuTTY 的私钥行。标签组总是参与匹配
# （` BLOCK` 可以是空串）：Python 里反向引用一个没参与匹配的组永远不成立。
_PEM = (
    re.compile(r'-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY((?: BLOCK)?)-----'),
    re.compile(r'-----END ([A-Z0-9 ]*)PRIVATE KEY((?: BLOCK)?)-----'),
)
_RFC4716 = (
    re.compile(r'---- BEGIN ([A-Z0-9 ]*)PRIVATE KEY() ----'),
    re.compile(r'---- END ([A-Z0-9 ]*)PRIVATE KEY() ----'),
)
# 组 1 是私钥行本身：每行都要到行尾为止，`Private-MAC:` 那一行不算。
_PUTTY_PRIVATE = re.compile(
    r'Private-Lines: \d{1,9}\n([A-Za-z0-9+/=]+(?=\n|\Z)(?:\n[A-Za-z0-9+/=]+(?=\n|\Z))*)'
)
# 开头那一行后面跟着像密钥的行（中间可以隔着 `Proc-Type:` 这类头部行，至多 8 行：不设上限时，每个
# 开头都要把后面所有像头部的行重扫一遍，是平方时间）：截断的私钥照样是私钥，只提到开头那一行的文字
# 不是。组 1 是第一行密钥。
_KEY_MATERIAL = re.compile(r'(?:[ \t]*\n[A-Za-z-]+: [^\n]*){0,8}\s*\n([A-Za-z0-9+/=]{16,})')
_KEY_LINE = re.compile(r'^[A-Za-z0-9+/=]{40,}$', re.MULTILINE)
# JSON 里转义过的换行：私钥块按换行认，`\n` 写成两个字符时它就认不出来了。
_ESCAPED_NEWLINE = re.compile(r'\\n')
# `Authorization` 的各种写法：前面不要求词界，`\r\n` 之后、`HTTP_AUTHORIZATION`、
# `Proxy-Authorization` 都认；字典与 JSON 里带引号的键；`=`。凭证要像凭证（有数字、大写或
# `+/=._-`），否则 `Authorization: token required` 会把 required 换掉。
_AUTHORIZATION = re.compile(
    r'((?i:Authorization)[\'"]?\s*[:=]\s*[\'"]?(?i:Bearer|token|Basic|Bot)\s+[\'"]?)'
    r'(?=[^\s\'"\\]{0,200}[0-9A-Z+/=._-])[^\s\'"\\]{8,}'
)
# 不带方案名、直接写凭证的 `Authorization: <key>`：凭证要过 `_credential_value`。
_BARE_AUTHORIZATION = re.compile(
    r'((?i:Authorization)[\'"]?\s*[:=]\s*[\'"]?)([^\s\'"\\]{16,4096}+)'
)
# URL 里的 `user:pass@`：用户名可以是空的（`redis://:pw@`），密码里可以有 `@`（取到最后一个）；
# JSON 里转义过的 `https:\/\/` 也算。scheme 限长、前面不许紧贴字母数字：一长串 `a.a.a.` 的每个起点
# 都只试有限几步，扫描才是线性的；diff 行里的 `+https://` 照样认得。
_URL_USERINFO = re.compile(
    r'((?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+.-]{0,31}:(?:\\?/){2})[^\s/@:]*:[^\s/]+@'
)
# 名字像密钥的赋值：`NAME=值`、`NAME: 值`、`"name": "值"`、`name = '值'`、`--api-key=值`、
# `apiKey := "值"`、`'api_key' => '值'`、`$apiKey = '值'`、`os.environ["API_KEY"] = "值"`。名字前面
# 不许紧贴名字字符（不从标识符中间起头）；不带引号的值里 `=` 只在末尾（base64 的补位）：
# `--env=API_KEY=值` 里外层的值于是到 `API_KEY` 就断了，里层的赋值才认得出来，`a=a=a=…` 也每个起点
# 一步就失败。值后面紧跟 `(`、`[` 的是表达式不是值。名字与值都限长、不回溯：每个起点的尝试有上限。
_ASSIGNMENT = re.compile(
    r'(?:["\'](?P<quoted_name>[A-Za-z_][A-Za-z0-9_.-]{0,100}+)["\']\]?'
    r'|(?<![A-Za-z0-9_.])(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,100}+))'
    r'[ \t]*(?::=|=>|[:=])[ \t]*'
    r'(?:["\'](?P<quoted_value>[^"\'\\\s]{16,4096}+)["\']'
    r'|(?P<value>[A-Za-z0-9+/_.~-]{16,4096}+={0,2})(?![(\[]))'
)
# Kubernetes / ECS 的环境变量表：名字与值分在两个字段里。
_ENV_PAIR = re.compile(
    r'name[\'"]?[ \t]*:[ \t]*[\'"]?(?P<name>[A-Za-z_][A-Za-z0-9_.-]{0,100}+)[\'"]?[ \t]*'
    r'(?:,[ \t]*|\n[ \t-]*)[\'"]?value[\'"]?[ \t]*:[ \t]*[\'"]?(?P<value>[^\s"\'\\,}]{16,4096}+)'
)
# 「值像凭证」：没有固定格式的密钥（`.env` 里的 `sk_live_…`、AWS secret key）只能按形状认。门槛
# 按「在 `src/` 与测试固定装置里零命中」定：只看名字与长度时，`key='llm_retry_attempts'` 这类写法
# 在源码里有几十处。
_BARE_CREDENTIAL = re.compile(r'[A-Za-z0-9+/=_.~-]{16,4096}')
_QUOTED_CREDENTIAL = re.compile(r'[^\s"\'\\]{16,4096}')
_VERSION = re.compile(r'v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]*)?')
_FILE_NAME = re.compile(r'\.[A-Za-z][A-Za-z0-9]{0,4}\Z')
_IDENTIFIER_PATH = re.compile(
    r'(?:[a-z_][a-z0-9_]*|[A-Z_][A-Z0-9_]*)(?:\.(?:[a-z_][a-z0-9_]*|[A-Z_][A-Z0-9_]*))+'
)
_SLUG = re.compile(r'[a-z0-9]{1,8}(?:[-_][a-z0-9]{1,8}){2,}')
_RESOURCE_PATH = re.compile(r'[a-z0-9_.-]+(?:/[a-z0-9_.-]+){2,}')
_DIGEST_PREFIX = re.compile(r'(?i)sha(?:1|224|256|384|512)[-:]')
_HEX = re.compile(r'[0-9A-Fa-f]+')
_EXAMPLE_WORDS = ('example', 'placeholder', 'xxxx', 'changeme', 'your', 'dummy')
_MIN_CREDENTIAL_ENTROPY = 3.5
# 十六进制的每字符熵至多 4 比特，随机的 32 位十六进制串也常在 3.5 以下。
_MIN_HEX_ENTROPY = 3.0
_MIN_HEX_LENGTH = 24
_RULE_ID = re.compile(r'custom\.[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}')
_AUTHORIZATION_KEY = re.compile(r'(?i)(?:proxy-)?authorization')
_FRAMED = 'Authorization: '
# `json.dumps(ensure_ascii=False)` 原样留在字符串里的字符：U+0085、U+2028、U+2029 让
# `str.splitlines()` 断行，DEL 与 C1 控制字符会被终端当成控制序列，落单的代理项编码不成 UTF-8。
# 换成 `\uXXXX` 之后解码回来的值不变。
_RAW_IN_JSON = re.compile('[\x7f-\x9f\u2028\u2029\ud800-\udfff]')
# 找区间的轮数上限：每一轮至少多换掉一个字符，正常输入两轮就找完；上限挡住刻意构造的长链。
_MAX_PASSES = 4
# 视图里的空白：不是空白字符、不是任何规则的值字符，也不是单词字符（`\b` 在它两边成立）。
_BLANK = '\x00'


def looks_secret(name: str) -> bool:
    """这个名字像不像装着密钥：按 `_`、`.`、`-` 切词，任一词是 KEY / TOKEN / AUTH 这类、以 KEY /
    PASSWORD 这类结尾，或相邻两词是 CONNECTION STRING。对名字的纯函数，不读环境。"""
    if name in _NOT_SECRET:
        return False
    words = [word.upper() for word in _NAME_PARTS.split(name)]
    if any(word in _SECRET_WORDS or word.endswith(_SECRET_SUFFIXES) for word in words):
        return True
    return any(pair in _SECRET_PAIRS for pair in zip(words, words[1:], strict=False))


@dataclass(frozen=True)
class Finding:
    """提交闸的一处命中：第几行（从 1 数）、哪条规则。不带命中内容，报告里不再出现一次密钥。"""

    line: int
    rule_id: str


@runtime_checkable
class Redactor(Protocol):
    """脱敏器：`redact` 交回换掉密钥的文本，对自己的输出再调一次结果不变；`scan` 是提交闸，只报
    行号与规则 ID。"""

    def redact(self, text: str) -> str: ...

    def scan(self, text: str) -> tuple[Finding, ...]: ...


_MINT = object()


@final
class RedactedText(str):
    """`encode_redacted` 的输出：每个字符串叶子都已脱敏、再做规范 JSON 编码的一行文本（没有任何
    `str.splitlines()` 会断开的字符，总能编码成 UTF-8）。只有 `encode_redacted` 构造它：收持久化
    内容的接口靠这个类型认出「已经逐叶脱敏」，所以直接调构造器（含 `type(x)(...)`）报
    `ContractViolation`。拼接、切片交回的是普通 `str`，改过的文本不再带这个类型；`copy` 交回它
    自己，`pickle` 读回的是普通 `str`。"""

    __slots__ = ()

    def __new__(cls, text: str, *, _mint: object = None) -> RedactedText:
        if _mint is not _MINT:
            raise ContractViolation(
                expected='RedactedText 只由 encode_redacted 构造',
                received=f'有代码直接构造了 {cls.__name__}',
                next_action='把值交给 encode_redacted(value, redactor)：它先逐叶脱敏再编码',
            )
        return super().__new__(cls, text)

    def __copy__(self) -> RedactedText:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> RedactedText:
        return self

    def __reduce__(self) -> tuple[type[str], tuple[str]]:
        return (str, (str(self),))


@dataclass(frozen=True)
class RedactionRule:
    """用户追加的规则：`rule_id` 以 `custom.` 开头，命中处换成 `[REDACTED:<rule_id>]`；`pattern` 是
    Python 正则，按原样编译（要忽略大小写就写 `(?i)`）。只有零宽命中的位置不换。"""

    rule_id: str
    pattern: str

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.rule_id), str):
            raise ConfigError(
                expected='用户规则的 id 是字符串',
                received=f'收到 {type(self.rule_id).__name__} 类型的 {self.rule_id!r}',
                next_action='写成 custom.<名字>，例如 custom.internal_host',
            )
        if self.rule_id in RULE_IDS:
            raise ConfigError(
                expected='用户规则的 id 不与内置规则重名',
                received=f'收到 id={self.rule_id!r}，它是内置规则',
                next_action='换一个以 custom. 开头的名字，例如 custom.internal_host；内置规则'
                '总是生效，不需要再声明',
            )
        if not _RULE_ID.fullmatch(self.rule_id):
            raise ConfigError(
                expected='用户规则的 id 以 custom. 开头，其余只含字母、数字、_ . -，'
                '不超过 107 个字符',
                received=f'收到 id={self.rule_id!r}',
                next_action='改成 custom.<名字>，例如 custom.internal_host：它会出现在占位符 '
                '[REDACTED:<id>] 里',
            )
        if not isinstance(cast(object, self.pattern), str) or not self.pattern:
            raise ConfigError(
                expected=f'用户规则 {self.rule_id} 的 pattern 是非空的正则字符串',
                received=f'收到 {type(self.pattern).__name__} 类型的 {self.pattern!r}',
                next_action='写成一个 Python 正则，例如 corp-[a-z0-9]+\\.internal',
            )
        if _compiled(self.rule_id, self.pattern).search('') is not None:
            raise ConfigError(
                expected=f'用户规则 {self.rule_id} 的 pattern 不能匹配空串',
                received=f'pattern {self.pattern!r} 能匹配空串',
                next_action='去掉让整个模式可以一个字符都不匹配的 * 与 ?，例如把 [a-z]* 改成 '
                '[a-z]+：匹配空串的规则会在每个位置插进一个占位符',
            )


def _compiled(rule_id: str, pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(
            expected=f'用户规则 {rule_id} 的 pattern 是合法的 Python 正则',
            received=f'pattern {pattern!r} 编译失败：{exc}',
            next_action='修正正则语法；要匹配字面的 . [ ( 这类字符就在前面加反斜杠',
        ) from exc


def _placeholder(rule_id: str) -> str:
    return f'[REDACTED:{rule_id}]'


def _masked(text: str) -> str:
    return _MASKED.sub(lambda match: ' ' * len(match.group()), text)


def _is_token(value: str) -> bool:
    """够长、没有空白的一个词。带 `/` 的只在没有 `.`、不是 URL 时算（base64 的密钥里常有 `/`），
    相对路径里的一段是普通文字。"""
    if len(value) < _MIN_TOKEN or any(char.isspace() for char in value):
        return False
    return '/' not in value or ('.' not in value and '://' not in value)


def _gateable(value: str) -> bool:
    """提交闸按逐字值拦的值：够长，而且至少混着两类字符。"""
    kinds = (str.islower, str.isupper, str.isdigit)
    mixed = sum(any(test(char) for char in value) for test in kinds)
    mixed += any(not char.isalnum() for char in value)
    return len(value) >= _MIN_GATED_VALUE and mixed >= 2


def _placeholder_key(token: str) -> bool:
    """文档里的示例 key 与占位（`AKIAIOSFODNN7EXAMPLE`、一串 x 或 0）：提交闸不拦。"""
    return 'EXAMPLE' in token.upper() or len(set(token)) < 6


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(count / len(value) * math.log2(count / len(value)) for count in counts.values())


def _credential_value(value: str, *, quoted: bool = False) -> bool:
    """一个值像不像凭证：16 到 4096 个凭证字符（带引号的值可以有符号），字母数字都有；不是带扩展名
    的路径或文件名、版本号、`settings.SECRET_KEY` 这种属性路径、`npm-cache-v2-linux` 这种短词串、
    `projects/x/secrets/y` 这种全小写的资源路径、`sha384-…` 这种摘要、示例；字符够杂（每字符熵至少
    3.5 比特，够长的十六进制串至少 3.0）。"""
    if not (_QUOTED_CREDENTIAL if quoted else _BARE_CREDENTIAL).fullmatch(value):
        return False
    if not any(char.isdigit() for char in value) or not any(char.isalpha() for char in value):
        return False
    if ('/' in value and '.' in value) or _FILE_NAME.search(value) or _VERSION.fullmatch(value):
        return False
    if _IDENTIFIER_PATH.fullmatch(value) or _SLUG.fullmatch(value) or _DIGEST_PREFIX.match(value):
        return False
    if _RESOURCE_PATH.fullmatch(value):
        return False
    lowered = value.lower()
    if any(word in lowered for word in _EXAMPLE_WORDS):
        return False
    hex_like = len(value) >= _MIN_HEX_LENGTH and _HEX.fullmatch(value) is not None
    return _entropy(value) >= (_MIN_HEX_ENTROPY if hex_like else _MIN_CREDENTIAL_ENTROPY)


def _key_blocks(
    text: str, markers: tuple[re.Pattern[str], re.Pattern[str]], *, strict: bool
) -> list[tuple[int, int]]:
    """私钥块的 (起, 止)。结束行先一趟收齐、按标签二分配对：一串没有结尾的开头逐个往后找是平方时间，
    而提交闸扫的 diff 是 Agent 写的。没有结束行而后面跟着密钥行的，一直算到末尾。`strict`（提交闸）
    只要真有一整行密钥的块：有结束行时中间任一行，没有时紧跟着的第一行——只有开头、结尾与 `...` 的
    文档示例不拦。"""
    begin_marker, end_marker = markers
    ends: dict[tuple[str, str], tuple[list[int], list[int]]] = {}
    for end in end_marker.finditer(text):
        starts, stops = ends.setdefault((end.group(1), end.group(2)), ([], []))
        starts.append(end.start())
        stops.append(end.end())
    blocks: list[tuple[int, int]] = []
    position = 0
    for begin in begin_marker.finditer(text):
        if begin.start() < position:
            continue
        starts, stops = ends.get((begin.group(1), begin.group(2)), ([], []))
        following = bisect.bisect_left(starts, begin.end())
        if following < len(starts):
            if not strict or _KEY_LINE.search(text, begin.end(), starts[following]):
                blocks.append((begin.start(), stops[following]))
            position = stops[following]
        elif material := _KEY_MATERIAL.match(text, begin.end()):
            if not strict or _KEY_LINE.match(text, material.start(1)):
                blocks.append((begin.start(), len(text)))
                position = len(text)
    return blocks


_Span = tuple[int, int, str]


def _key_block_spans(view: str) -> list[_Span]:
    blocks = _key_blocks(view, _PEM, strict=False)
    blocks += _key_blocks(view, _RFC4716, strict=False)
    blocks += [(hit.start(1), hit.end(1)) for hit in _PUTTY_PRIVATE.finditer(view)]
    return [(start, end, 'secret.private_key_block') for start, end in blocks]


def _authorization_spans(view: str) -> list[_Span]:
    spans = [
        (hit.end(1), hit.end(), 'secret.authorization_header')
        for hit in _AUTHORIZATION.finditer(view)
    ]
    spans += [
        (hit.start(2), hit.end(2), 'secret.authorization_header')
        for hit in _BARE_AUTHORIZATION.finditer(view)
        if _credential_value(hit.group(2), quoted=True)
    ]
    return spans


def _url_userinfo_spans(view: str) -> list[_Span]:
    return [
        (hit.end(1), hit.end() - 1, 'secret.url_userinfo') for hit in _URL_USERINFO.finditer(view)
    ]


def _format_spans(view: str) -> list[_Span]:
    masked = _masked(view)
    spans = [
        (hit.start(), hit.end(), rule_id)
        for rule_id, pattern in _FORMATS
        for hit in pattern.finditer(masked)
    ]
    pending = sorted({end for _, end, _ in spans})
    seen = set(pending)
    while pending:
        end = pending.pop()
        for rule_id, glued in _GLUED:
            if hit := glued.match(masked, end):
                spans.append((hit.start(), hit.end(), rule_id))
                if hit.end() not in seen:
                    seen.add(hit.end())
                    pending.append(hit.end())
    return spans


def _assignment_spans(view: str) -> list[_Span]:
    spans: list[_Span] = []
    for hit in _ASSIGNMENT.finditer(view):
        name = hit.group('quoted_name') or hit.group('name')
        quoted = hit.group('quoted_value') is not None
        group = 'quoted_value' if quoted else 'value'
        if looks_secret(name) and _credential_value(hit.group(group), quoted=quoted):
            spans.append((hit.start(group), hit.end(group), 'secret.named_assignment'))
    for hit in _ENV_PAIR.finditer(view):
        if looks_secret(hit.group('name')) and _credential_value(hit.group('value'), quoted=True):
            spans.append((hit.start('value'), hit.end('value'), 'secret.named_assignment'))
    return spans


def _trimmed(span: _Span, blank: bytearray) -> _Span | None:
    """去掉区间两头已经是空白的部分；整段都是空白的交回 None。区间因此从不只盖住一个占位符的一半，
    一个区间也只在它盖住了还没换掉的字符时才算新找到的。"""
    start, end, rule_id = span
    while start < end and blank[start]:
        start += 1
    while end > start and blank[end - 1]:
        end -= 1
    return (start, end, rule_id) if end > start else None


def _view(text: str, blank: bytearray, fill: str) -> str:
    """把 `blank` 标记的位置换成 `fill`（等长，位置不变）。"""
    if blank.find(1) == -1:
        return text
    pieces: list[str] = []
    position = 0
    for run in re.finditer(rb'\x01+', blank):
        pieces.append(text[position : run.start()])
        pieces.append(fill * (run.end() - run.start()))
        position = run.end()
    pieces.append(text[position:])
    return ''.join(pieces)


class RuleRedactor:
    """内置规则加用户规则的脱敏器。`secrets` 是要逐字换掉的密钥原值（宿主从自己的环境里挑好；太短的
    值会把正文里同样的词一起换掉，挑的时候就该去掉）；`rules` 的命中与内置规则的命中重叠时，换成
    覆盖更长的那一个，一样长时内置规则优先，用户规则之间按给出的顺序。"""

    def __init__(self, *, secrets: Iterable[str] = (), rules: Sequence[RedactionRule] = ()) -> None:
        self._values = _validated_secrets(secrets)
        self._rules = _validated_rules(rules)
        self._custom = tuple((rule.rule_id, re.compile(rule.pattern)) for rule in self._rules)
        ids = (*_PRECEDENCE, *(rule.rule_id for rule in self._rules))
        self._order = {rule_id: index for index, rule_id in enumerate(ids)}
        self._opaque = re.compile('|'.join(re.escape(_placeholder(rule_id)) for rule_id in ids))
        self._gated = tuple(value for value in self._values if _gateable(value))
        windows: set[str] = set()
        for value in self._values:
            if _is_token(value):
                prefix = _PUBLIC_PREFIX.match(value)
                core = value[prefix.end() :] if prefix else value
                windows.update(
                    core[start : start + _MIN_FRAGMENT]
                    for start in range(len(core) - _MIN_FRAGMENT + 1)
                )
        self._windows = tuple(sorted(windows))

    @property
    def rules(self) -> tuple[RedactionRule, ...]:
        """用户规则，按给出的顺序。"""
        return self._rules

    @property
    def digest(self) -> str:
        """规则集的 sha256：`RULES_VERSION`、内置规则 ID 的顺序与用户规则的 (id, pattern)。不含
        `secrets`——它们是宿主环境里的值，不能进 trace。"""
        ruleset = {
            'builtin': list(RULE_IDS),
            'custom': [[rule.rule_id, rule.pattern] for rule in self._rules],
            'rules_version': RULES_VERSION,
        }
        canonical = json.dumps(ruleset, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def redact(self, text: str) -> str:
        """全部规则，含令牌片段、名字像密钥的赋值与用户规则。"""
        _require_text(text, 'redact')
        blank = bytearray(len(text))
        for hit in self._opaque.finditer(text):
            blank[hit.start() : hit.end()] = b'\x01' * (hit.end() - hit.start())
        spans: list[_Span] = []
        for _ in range(_MAX_PASSES):
            found = [kept for span in self._spans(text, blank) if (kept := _trimmed(span, blank))]
            if not found:
                break
            spans += found
            for start, end, _ in found:
                blank[start:end] = b'\x01' * (end - start)
        return self._rewritten(text, spans)

    def scan(self, text: str) -> tuple[Finding, ...]:
        """提交闸：够长够杂的逐字值、格式规则（示例与占位除外）与真有密钥行的私钥；不做片段匹配，也不认
        `Authorization:` 与 URL 里的 `user:pass@`——文档里的 `Bearer $TOKEN`、
        `https://user:pass@example.com` 不是泄露。调用方只交新增的行：删掉一个泄露的 key 不该被
        拦。"""
        _require_text(text, 'scan')
        found: dict[Finding, None] = {}
        for number, (line, masked) in enumerate(
            zip(text.split('\n'), _masked(text).split('\n'), strict=True), start=1
        ):
            if any(value in line for value in self._gated):
                found[Finding(number, 'secret.env_value')] = None
            for rule_id, pattern in _FORMATS:
                if any(not _placeholder_key(hit.group()) for hit in pattern.finditer(masked)):
                    found[Finding(number, rule_id)] = None
        starts = [start for start, _ in _key_blocks(text, _PEM, strict=True)]
        starts += [start for start, _ in _key_blocks(text, _RFC4716, strict=True)]
        starts += [
            hit.start()
            for hit in _PUTTY_PRIVATE.finditer(text)
            if _KEY_LINE.search(text, hit.start(1), hit.end(1))
        ]
        for start in starts:
            found[Finding(text.count('\n', 0, start) + 1, 'secret.private_key_block')] = None
        return tuple(sorted(found, key=lambda finding: (finding.line, finding.rule_id)))

    def _spans(self, text: str, blank: bytearray) -> list[_Span]:
        """每条规则在视图上找到的区间。私钥块在把空白看成空格、把 JSON 转义的 `\\n` 看成换行的视图上
        找：块里一行先被换掉、或整块写在 JSON 字符串里时，它照样是一个块。"""
        view = _view(text, blank, _BLANK)
        spaced = _ESCAPED_NEWLINE.sub(' \n', _view(text, blank, ' '))
        env = 'secret.env_value'
        spans: list[_Span] = []
        for value in self._values:
            start = view.find(value)
            while start != -1:
                spans.append((start, start + len(value), env))
                start = view.find(value, start + 1)
        spans += self._fragment_spans(view)
        spans += _key_block_spans(spaced)
        spans += _authorization_spans(view)
        spans += _url_userinfo_spans(view)
        spans += _format_spans(view)
        spans += _assignment_spans(view)
        for rule_id, pattern in self._custom:
            spans += [(hit.start(), hit.end(), rule_id) for hit in pattern.finditer(view)]
        return spans

    def _fragment_spans(self, view: str) -> list[_Span]:
        """令牌里 16 个字符以上的任意一段：长的片段由若干个 16 字符窗口首尾相叠而成，逐个窗口找、在
        一张逐字节的标记表上涂掉，再按连续的一段交出——线性时间，内存是文本长度的一个字节。"""
        mask: bytearray | None = None
        cover = b'\x01' * _MIN_FRAGMENT
        for window in self._windows:
            start = view.find(window)
            while start != -1:
                if mask is None:
                    mask = bytearray(len(view))
                mask[start : start + _MIN_FRAGMENT] = cover
                start = view.find(window, start + 1)
        if mask is None:
            return []
        return [
            (run.start(), run.end(), 'secret.env_value') for run in re.finditer(rb'\x01+', mask)
        ]

    def _rewritten(self, text: str, spans: list[_Span]) -> str:
        """相互重叠的区间并成一段，换成其中最长那一条规则的占位符；一样长时按 `_PRECEDENCE`，用户
        规则之间按给出的顺序。"""
        if not spans:
            return text

        def rank(span: _Span) -> tuple[int, int]:
            return (span[0] - span[1], self._order[span[2]])

        ordered = sorted(spans, key=lambda span: (span[0], *rank(span)))
        pieces: list[str] = []
        position = 0
        start, end, best = ordered[0][0], ordered[0][1], ordered[0]
        for span in ordered[1:]:
            if span[0] < end:
                end = max(end, span[1])
                best = min(best, span, key=rank)
                continue
            pieces += [text[position:start], _placeholder(best[2])]
            position = end
            start, end, best = span[0], span[1], span
        pieces += [text[position:start], _placeholder(best[2]), text[end:]]
        return ''.join(pieces)


def _require_text(text: str, method: str) -> None:
    if not isinstance(cast(object, text), str):
        raise ContractViolation(
            expected=f'{method} 收一段 str',
            received=f'收到 {type(text).__name__}',
            next_action='bytes 先解码成 str 再交进来；结构化的值用 encode_redacted 编码',
        )


def _validated_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    given = cast(object, secrets)
    if isinstance(given, str | bytes | Mapping) or not isinstance(given, Iterable):
        raise ConfigError(
            expected='secrets 是密钥原值的列表、元组或集合',
            received=f'收到 {type(secrets).__name__}',
            next_action='把要换掉的值放进列表，例如 secrets=[os.environ["API_KEY"]]：一个字符串会'
            '被逐字符拆开，一个映射只交出它的键（变量名）',
        )
    values: set[str] = set()
    for index, value in enumerate(cast(Iterable[object], given)):
        if not isinstance(value, str) or not value:
            raise ConfigError(
                expected='secrets 的每一项是非空字符串（要逐字换掉的密钥原值）',
                received=f'secrets[{index}] 是'
                + ('空串' if isinstance(value, str) else f' {type(value).__name__}'),
                next_action='去掉空串与非字符串：空串会在每个位置插进一个占位符',
            )
        values.add(value)
    return tuple(sorted(values))


def _validated_rules(rules: Sequence[RedactionRule]) -> tuple[RedactionRule, ...]:
    given = cast(object, rules)
    if isinstance(given, str | bytes) or not isinstance(given, Sequence):
        raise ConfigError(
            expected='rules 是 RedactionRule 的序列',
            received=f'收到 {type(rules).__name__}',
            next_action='传一个列表或元组，例如 '
            'rules=[RedactionRule("custom.host", "corp-[a-z]+")]',
        )
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(cast(object, rule), RedactionRule):
            raise ConfigError(
                expected='rules 的每一项是 RedactionRule',
                received=f'rules[{index}] 是 {type(rule).__name__}',
                next_action='用 RedactionRule(rule_id, pattern) 构造每一条',
            )
        if rule.rule_id in seen:
            raise ConfigError(
                expected='用户规则的 id 互不重复',
                received=f'{rule.rule_id!r} 出现了不止一次',
                next_action='合并成一条（正则里用 | 连接），或给其中一条换个名字',
            )
        seen.add(rule.rule_id)
    placeholders = [_placeholder(rule_id) for rule_id in (*RULE_IDS, *(r.rule_id for r in rules))]
    for rule in rules:
        compiled = re.compile(rule.pattern)
        hit = next((text for text in placeholders if compiled.search(text)), None)
        if hit is not None:
            raise ConfigError(
                expected=f'用户规则 {rule.rule_id} 的 pattern 不匹配任何占位符',
                received=f'pattern {rule.pattern!r} 能匹配 {hit!r}',
                next_action='收紧正则，让它只匹配你要藏起来的内容：能匹配占位符的规则会把别的规则'
                '已经换好的地方再换一遍',
            )
    return tuple(rules)


def encode_redacted(value: object, redactor: Redactor) -> RedactedText:
    """先对每个字符串叶子（含映射的键）分别脱敏，再做规范 JSON 编码（`ensure_ascii=False`、键排序、
    无多余空白）。逐叶脱敏看不见键与值的关系，所以映射另按键判：名字像密钥的键下面（任意层，含列表
    与嵌套的映射），脱敏之后仍像凭证的字符串叶子换成 `secret.named_assignment` 的占位符；
    `Authorization` 这类键的值按 `Authorization:` 头脱敏；`{"name": <像密钥的名字>, "value": …}`
    这种环境变量表按名字判值；两项的字符串列表当成一对键值。两个键脱敏之后撞成同一个时，按原键排序
    后来的那个加 `#2`、`#3`，不丢任何一项。只收 JSON 能表达的值：dict（键是字符串）、list、tuple、
    str、int、float（有限）、bool、None。"""
    if not isinstance(cast(object, redactor), Redactor):
        raise ContractViolation(
            expected='encode_redacted 的第二个参数是脱敏器（有 redact 与 scan 方法）',
            received=f'收到 {type(redactor).__name__}',
            next_action='传一个 RuleRedactor，例如 RuleRedactor(secrets=[...])',
        )
    try:
        tree = _redacted(value, redactor, '$', secret=False)
    except RecursionError as exc:
        raise ContractViolation(
            expected='encode_redacted 的值嵌套层数在 Python 的递归上限之内',
            received='嵌套太深，逐层脱敏时超过了递归上限',
            next_action='把它拍平或截断之后再编码；trace 不需要几百层的嵌套',
        ) from exc
    try:
        text = json.dumps(
            tree, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
        )
    except ValueError as exc:
        raise ContractViolation(
            expected='encode_redacted 的值能编码成 JSON',
            received=f'编码失败：{exc}',
            next_action='把过大的整数转成字符串之后再编码',
        ) from exc
    escaped = _RAW_IN_JSON.sub(lambda match: f'\\u{ord(match.group()):04x}', text)
    return RedactedText(escaped, _mint=_MINT)


def write_all(fd: int, data: bytes) -> None:
    """把 `data` 完整写进 `fd`。`os.write` 可以只写进一部分而不报错（文件大小上限、磁盘将满）：
    只调一次会留下一个被截断、却没有任何报错的文件。写入点守卫把它当 `os.write` 看，内容照样逐个
    检查。"""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError(
                errno.EIO,
                '期望 os.write 至少写进一个字节；实际写进 0 个。'
                '腾出磁盘空间或调高文件大小上限再重试',
            )
        view = view[written:]


def write_redacted(fd: int, text: RedactedText, *, newline: bool = False) -> None:
    """把一段 `RedactedText` 完整写进 `fd`（`newline` 时再写一个换行：它保证只有一行）。运行时检查
    类型：静态类型挡不住 `Any`，从 JSON 里读出来的值就是 `Any`。"""
    if not isinstance(cast(object, text), RedactedText):
        raise ContractViolation(
            expected='write_redacted 只写 RedactedText（逐叶脱敏之后的编码）',
            received=f'收到 {type(text).__name__}',
            next_action='先经 encode_redacted 编码再写；普通 str 可能带着密钥',
        )
    write_all(fd, text.encode('utf-8') + (b'\n' if newline else b''))


def _redacted(value: object, redactor: Redactor, path: str, *, secret: bool) -> object:
    """`secret`：这个值在一个名字像密钥的键下面。`path` 只进错误信息，里面的键已经脱敏。"""
    if isinstance(value, str):
        text = redactor.redact(value)
        if secret and _credential_value(text, quoted=True):
            return _placeholder('secret.named_assignment')
        return text
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractViolation(
            expected='encode_redacted 的数值是有限数',
            received=f'{path} 是 {value!r}',
            next_action='把它换成 None 或字符串之后再编码：JSON 表达不了 NaN 与无穷大',
        )
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return _redacted_mapping(cast(Mapping[object, object], value), redactor, path, secret)
    if isinstance(value, list | tuple):
        items = cast(Sequence[object], value)
        if len(items) == 2 and isinstance(items[0], str) and isinstance(items[1], str):
            return [
                _redacted(items[0], redactor, f'{path}[0]', secret=secret),
                _entry(items[0], items[1], redactor, f'{path}[1]', secret=secret),
            ]
        return [
            _redacted(item, redactor, f'{path}[{index}]', secret=secret)
            for index, item in enumerate(items)
        ]
    raise ContractViolation(
        expected='encode_redacted 只收 JSON 能表达的值：dict、list、tuple、str、int、float、'
        'bool、None',
        received=f'{path} 是 {type(value).__name__}',
        next_action='先把它转成上面这些类型（dataclass 用 dataclasses.asdict，枚举取 .value）',
    )


def _redacted_mapping(
    value: Mapping[object, object], redactor: Redactor, path: str, secret: bool
) -> dict[str, object]:
    for key in value:
        if not isinstance(key, str):
            raise ContractViolation(
                expected='encode_redacted 的映射键都是字符串',
                received=f'{path} 里有 {type(key).__name__} 类型的键',
                next_action='把键转成字符串之后再编码：JSON 的键只能是字符串',
            )
    entries = sorted(cast(Mapping[str, object], value).items())
    pair_name = value.get('name')
    secret_pair = isinstance(pair_name, str) and looks_secret(pair_name)
    result: dict[str, object] = {}
    for key, item in entries:
        name = redactor.redact(key)
        inherited = secret or (key == 'value' and secret_pair)
        redacted = _entry(key, item, redactor, f'{path}.{name}', secret=inherited)
        unique = name
        suffix = 2
        while unique in result:
            unique = f'{name}#{suffix}'
            suffix += 1
        result[unique] = redacted
    return result


def _entry(key: str, item: object, redactor: Redactor, path: str, *, secret: bool) -> object:
    """映射里一项（或一对键值）的值：`Authorization` 这类键按头脱敏；名字像密钥的键让它下面的值
    都按 `secret` 判。"""
    if isinstance(item, str) and _AUTHORIZATION_KEY.fullmatch(key):
        framed = redactor.redact(_FRAMED + item)
        if framed.startswith(_FRAMED):
            return framed[len(_FRAMED) :]
        return redactor.redact(item)
    return _redacted(item, redactor, path, secret=secret or looks_secret(key))
