"""按字符数截文本，不让刀口留下一截认不出的凭证（spec §12.3）。

交给模型、写进轨迹的文本往往先截断、落进 trace 与会话时才脱敏，而脱敏只认得完整的凭证：有的靠格式
（`sk-…`），有的靠上下文（`NAME=值` 的名字、`Authorization:`、私钥块的开头一行）。所以刀口两条规则：

- 落进内置规则认凭证时用到的一整段（`redaction.credential_regions`，上下文加值）里，就把整段丢到刀口
  那一侧——留下的要么连同上下文完整在，要么一个字也不在；
- 落在一串凭证字符（ASCII 字母数字与 `-_./+~%:@!#$^*?`）中间，就把这一侧连着的那串整串丢掉——兜住规则
  认不出、只有宿主知道的那些值。

刀口另一侧看不见（被丢弃的那段字节）时：结尾退到最近的空白（引号里的密码可以带 `&` `;` 这类
字符，只丢凭证字符会留下半截）；开头那半行整行不留（它可能只剩名字的后半加上完整的值），剩着的
私钥块后半连同它的结尾那一行一起丢。开头那一行比 `_LINE_CAP` 还长时退回到最近的空白。
"""

from __future__ import annotations

import string

from syndica.kernel.redaction import credential_regions, orphan_key_tail

__all__ = ['clip_head', 'clip_tail', 'drop_cut_end', 'drop_cut_start']

_CREDENTIAL = frozenset(string.ascii_letters + string.digits + '-_./+~%:@!#$^*?')
_LINE_CAP = 4096


def _inside(char: str) -> bool:
    return char in _CREDENTIAL


def clip_head(text: str, limit: int) -> str:
    """`text` 开头至多 `limit` 个字符；不用截时原样交回。"""
    if len(text) <= limit:
        return text
    regions = credential_regions(text)
    end = max(limit, 0)
    while True:
        moved = end
        for start, stop in regions:
            if start < moved < stop:
                moved = start
        if 0 < moved < len(text) and _inside(text[moved - 1]) and _inside(text[moved]):
            while moved and _inside(text[moved - 1]):
                moved -= 1
        if moved == end:
            return text[:end]
        end = moved


def clip_tail(text: str, limit: int) -> str:
    """`text` 结尾至多 `limit` 个字符；不用截时原样交回。"""
    if len(text) <= limit:
        return text
    regions = credential_regions(text)
    begin = len(text) - max(limit, 0)
    while True:
        moved = begin
        for start, stop in regions:
            if start < moved < stop:
                moved = stop
        if 0 < moved < len(text) and _inside(text[moved - 1]) and _inside(text[moved]):
            while moved < len(text) and _inside(text[moved]):
                moved += 1
        if moved == begin:
            return text[begin:]
        begin = moved


def drop_cut_end(text: str) -> str:
    """`text` 之后的字符被丢掉了、看不到：退到最近的空白。上下文都在值的前面，切掉结尾只会丢掉值的
    一部分，而值里没有空白。"""
    end = len(text)
    while end and not text[end - 1].isspace():
        end -= 1
    return text[:end]


def drop_cut_start(text: str) -> str:
    """`text` 之前的字符被丢掉了、看不到：开头那半行不留，开头剩着的私钥块后半连同结尾那一行
    不留。"""
    newline = text.find('\n')
    if newline != -1 and newline < _LINE_CAP:
        begin = newline + 1
    else:
        begin = 0
        while begin < len(text) and not text[begin].isspace():
            begin += 1
    rest = text[begin:]
    return rest[orphan_key_tail(rest) :]
