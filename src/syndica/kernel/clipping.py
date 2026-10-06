"""按字符数截文本，不在一个可能是凭证的词中间下刀（spec §12.3）。

脱敏只认得完整的凭证，而交给模型、写进轨迹的文本往往先截断，落进 trace 与会话时才脱敏：刀口切在
一个密钥中间，留下的那一截脱敏器认不出来，就原样写进磁盘。刀口两侧都是凭证字符时，这一侧连着的那串
凭证字符整串丢掉，于是跨过刀口的凭证一个字符也不留，没跨过的完整留下、照常被脱敏器认出。

凭证字符只有 ASCII 字母数字与 `-_./+~%:@!#$^*?`：引号、括号、逗号、等号、空白与一切非 ASCII 字符
都不可能在凭证里，所以中文正文、带分隔符的 JSON 刀口两侧照常保留，只有一长串没有分隔的 base64 会被
整串丢掉。
"""

from __future__ import annotations

import string

__all__ = ['clip_head', 'clip_tail', 'drop_cut_end', 'drop_cut_start']

_CREDENTIAL = frozenset(string.ascii_letters + string.digits + '-_./+~%:@!#$^*?')


def _inside(char: str) -> bool:
    return char in _CREDENTIAL


def clip_head(text: str, limit: int) -> str:
    """`text` 开头至多 `limit` 个字符；不用截时原样交回。"""
    if len(text) <= limit:
        return text
    end = max(limit, 0)
    if end and _inside(text[end - 1]) and _inside(text[end]):
        while end and _inside(text[end - 1]):
            end -= 1
    return text[:end]


def clip_tail(text: str, limit: int) -> str:
    """`text` 结尾至多 `limit` 个字符；不用截时原样交回。"""
    if len(text) <= limit:
        return text
    start = len(text) - max(limit, 0)
    if start < len(text) and _inside(text[start - 1]) and _inside(text[start]):
        while start < len(text) and _inside(text[start]):
            start += 1
    return text[start:]


def drop_cut_end(text: str) -> str:
    """`text` 之后的字符被丢掉了、看不到时：末尾那串凭证字符可能是半个凭证，整串去掉。"""
    end = len(text)
    while end and _inside(text[end - 1]):
        end -= 1
    return text[:end]


def drop_cut_start(text: str) -> str:
    """`text` 之前的字符被丢掉了、看不到时：开头那串凭证字符可能是半个凭证，整串去掉。"""
    start = 0
    while start < len(text) and _inside(text[start]):
        start += 1
    return text[start:]
