"""进终端之前的转义（spec D13）：来自模型、Agent 或文件系统的字符串都经 `terminal_safe`。"""

from __future__ import annotations

import re

__all__ = ['one_line', 'terminal_safe']

# 除换行与制表符外的 C0、DEL、C1、双向文字控制字符、U+2028 / U+2029，以及落单的代理字符：被
# `max_tokens` 截断的 emoji 就是一个落单的高位代理，它编码成 UTF-8 时抛错。
_UNSAFE = re.compile(
    '[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029'
    '\ud800-\udfff]'
)


def _escaped(match: re.Match[str]) -> str:
    code = ord(match.group())
    return f'\\x{code:02x}' if code <= 0xFF else f'\\u{code:04x}'


def terminal_safe(text: str) -> str:
    """这些字符转义成可见的 `\\x..` / `\\u....`：模型写的文件名或结论不能在终端上改写前面的输出，
    也不能把一段文字倒着显示。换行与制表符保留。"""
    return _UNSAFE.sub(_escaped, text)


def one_line(text: str) -> str:
    """只该占一行的字符串（文件名、命令）：换行与制表符也转义，否则一个带换行的文件名能在交接
    文件里伪造出一节。"""
    return terminal_safe(text).replace('\n', '\\n').replace('\t', '\\t')
