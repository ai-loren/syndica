"""最小脱敏器（spec D12）：handoff、`--handoff` 下的两条流与提交消息落盘之前经它替换。

M3 建统一 `Redactor` 时并进去。规则各有稳定 ID，命中处换成 `[REDACTED:<规则 ID>]`，不留原文。
`scan` 是提交闸用的另一种模式：只报行号与规则 ID、不交回命中内容，而且不做片段匹配——片段匹配允许
误报，拦提交不允许。

先脱敏再截断：截到 72 个字符的提交标题会把一个完整格式的 key 截成认不出的一段。一段文字分几次交进来
（流式写入）时，调用方按行攒齐再交：跨两次调用的 key 哪一次都认不出。
"""

from __future__ import annotations

import bisect
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from syndica.cli.environment import looks_secret

__all__ = ['RULE_IDS', 'Finding', 'Redactor']

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
)

# 值短于这个长度不当凭证：`PASS=on` 这种开关按值替换会把正文里每个 `on` 都换掉。
_MIN_VALUE = 8
# 令牌另按片段认：被截断的 key 也要换掉。片段短于 16 个字符时，普通词与标识符会撞上随机令牌里的
# 一段。
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
_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('secret.openrouter_key', re.compile(r'\bsk-or-v1-[0-9a-f]{64}')),
    (
        'secret.openai_key',
        # 真的 key 是随机串，有数字也有大写：只由小写与连字符组成的长词（CSS 类名）不是。前瞻限长，
        # 一长串 `sk-sk-sk-…` 才不会让每个起点都扫到末尾。
        re.compile(
            r'\bsk-(?=[A-Za-z0-9_-]{0,200}\d)(?=[A-Za-z0-9_-]{0,200}[A-Z])[A-Za-z0-9_-]{40,}'
        ),
    ),
    ('secret.google_api_key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}')),
    (
        'secret.github_token',
        re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})'),
    ),
    ('secret.aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}')),
    (
        'secret.slack_token',
        re.compile(
            r'\b(?:xox[abprs]-\d{8,}-[A-Za-z0-9-]{16,}|xoxe\.xox[bp]-\d-[A-Za-z0-9-]{20,}'
            r'|xapp-\d-[A-Za-z0-9-]{20,})'
        ),
    ),
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
# 开头那一行后面跟着像密钥的行（中间可以隔着 `Proc-Type:` 这类头部行）：截断的私钥照样是私钥，只提到
# 开头那一行的文字不是。组 1 是第一行密钥。
_KEY_MATERIAL = re.compile(r'(?:[ \t]*\n[A-Za-z-]+: [^\n]*)*\s*\n([A-Za-z0-9+/=]{16,})')
_KEY_LINE = re.compile(r'^[A-Za-z0-9+/=]{40,}$', re.MULTILINE)
# `Authorization` 的各种写法：前面不要求词界，`\r\n` 之后、`HTTP_AUTHORIZATION`、
# `Proxy-Authorization` 都认；字典与 JSON 里带引号的键；`=`。凭证要像凭证（有数字、大写或
# `+/=._-`），否则 `Authorization: token required` 会把 required 换掉。
_AUTHORIZATION = re.compile(
    r'((?i:Authorization)[\'"]?\s*[:=]\s*[\'"]?(?i:Bearer|token|Basic)\s+[\'"]?)'
    r'(?=[^\s\'"\\]{0,200}[0-9A-Z+/=._-])[^\s\'"\\]{8,}'
)
# URL 里的 `user:pass@`：用户名可以是空的（`redis://:pw@`），密码里可以有 `@`（取到最后一个）；
# JSON 里转义过的 `https:\/\/` 也算。scheme 前面不许紧贴 scheme 字符：否则一长串 `a.a.a.` 的每个
# 起点都重新匹配一次，是平方时间。
_URL_USERINFO = re.compile(
    r'((?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*:(?:\\?/){2})[^\s/@:]*:[^\s/]+@'
)


@dataclass(frozen=True)
class Finding:
    """提交闸的一处命中：第几行（从 1 数）、哪条规则。不带命中内容，报告里不再出现一次密钥。"""

    line: int
    rule_id: str


def _placeholder(rule_id: str) -> str:
    return f'[REDACTED:{rule_id}]'


def _masked(text: str) -> str:
    return _MASKED.sub(lambda match: ' ' * len(match.group()), text)


def _is_path(value: str) -> bool:
    """真的存在的绝对路径（`SSH_AUTH_SOCK`）。按「以 `/` 开头」判会漏掉以 `/` 开头的 base64 密钥
    （64 个里就有一个）。"""
    return value.startswith('/') and os.path.exists(value)


def _credential_like(value: str) -> bool:
    """值像凭证：够长，不是存在的绝对路径，不是不带凭证的 URL。"""
    if len(value) < _MIN_VALUE or _is_path(value):
        return False
    if re.match(r'[A-Za-z][A-Za-z0-9+.-]*://', value):
        try:
            parts = urlsplit(value)
        except ValueError:
            return True
        return bool(parts.username or parts.password)
    return True


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


def _replaced(text: str, spans: list[tuple[int, int, str]]) -> str:
    """把互不重叠的 (起, 止, 规则 ID) 换成占位；重叠时先开始的那一段为准。"""
    pieces: list[str] = []
    position = 0
    for start, end, rule_id in sorted(spans):
        if start >= position:
            pieces.append(text[position:start])
            pieces.append(_placeholder(rule_id))
            position = end
    pieces.append(text[position:])
    return ''.join(pieces)


class Redactor:
    """构造时读一次宿主环境里的密钥值（名字按 D6 判为密钥、值像凭证）；之后 `redact` 与 `scan` 只看
    交进来的文本。"""

    def __init__(self, environ: Mapping[str, str]) -> None:
        values: set[str] = set()
        for name, value in environ.items():
            if looks_secret(name):
                # 经 `echo | base64`、Kubernetes secret 传进来的值常带着换行，正文里出现的是去掉
                # 它的那一份。
                values.update(v for v in {value, value.strip()} if _credential_like(v))
        # 长的先换：短值是长值的一段时，先换短的会在长值里留下它剩下的部分。
        self._values = tuple(sorted(values, key=lambda value: (-len(value), value)))
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

    def redact(self, text: str) -> str:
        """全部规则，含令牌片段。"""
        env = _placeholder('secret.env_value')
        for value in self._values:
            text = text.replace(value, env)
        text = self._without_fragments(text)
        blocks = _key_blocks(text, _PEM, strict=False)
        blocks += _key_blocks(text, _RFC4716, strict=False)
        blocks += [(hit.start(1), hit.end(1)) for hit in _PUTTY_PRIVATE.finditer(text)]
        text = _replaced(text, [(start, end, 'secret.private_key_block') for start, end in blocks])
        text = _AUTHORIZATION.sub(
            lambda match: match.group(1) + _placeholder('secret.authorization_header'), text
        )
        text = _URL_USERINFO.sub(
            lambda match: match.group(1) + _placeholder('secret.url_userinfo') + '@', text
        )
        return self._without_formats(text)

    def scan(self, text: str) -> tuple[Finding, ...]:
        """提交闸：够长够杂的逐字值、格式规则（示例与占位除外）与真有密钥行的私钥；不做片段匹配，也不认
        `Authorization:` 与 URL 里的 `user:pass@`——文档里的 `Bearer $TOKEN`、
        `https://user:pass@example.com` 不是泄露。调用方只交新增的行：删掉一个泄露的 key 不该被
        拦。"""
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

    def _without_formats(self, text: str) -> str:
        masked = _masked(text)
        return _replaced(
            text,
            [
                (hit.start(), hit.end(), rule_id)
                for rule_id, pattern in _FORMATS
                for hit in pattern.finditer(masked)
            ],
        )

    def _without_fragments(self, text: str) -> str:
        """换掉令牌里 16 个字符以上的任意一段：长的片段由若干个 16 字符窗口首尾相叠而成，逐个窗口
        找、在一张逐字节的标记表上涂掉，再按连续的一段换成一个占位——线性时间，内存是文本长度的
        一个字节。"""
        mask: bytearray | None = None
        cover = b'\x01' * _MIN_FRAGMENT
        for window in self._windows:
            start = text.find(window)
            while start != -1:
                if mask is None:
                    mask = bytearray(len(text))
                mask[start : start + _MIN_FRAGMENT] = cover
                start = text.find(window, start + 1)
        if mask is None:
            return text
        env = _placeholder('secret.env_value')
        pieces: list[str] = []
        position = 0
        for run in re.finditer(rb'\x01+', mask):
            pieces.append(text[position : run.start()])
            pieces.append(env)
            position = run.end()
        pieces.append(text[position:])
        return ''.join(pieces)
