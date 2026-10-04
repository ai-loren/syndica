"""会在终端上改写显示的字符。内核按它拒收第三方清单里要拿去给人看的字符串（Module 的说明、安全
说明、版本）；CLI 进终端之前按逐字相同的一份转义（`cli/terminal.py`，它不能为此加载内核）。

另有一个流式解码用的判据：还在产出的命令或终端输出，末尾半个 utf-8 字符要留到下一次再交出去。"""

from __future__ import annotations

import re

__all__ = ['UNSAFE_TEXT']

# 除换行与制表符外的 C0、DEL、C1、双向文字控制字符、U+2028 / U+2029，以及落单的代理字符：被
# `max_tokens` 截断的 emoji 就是一个落单的高位代理，它编码成 UTF-8 时抛错。
UNSAFE_TEXT = re.compile(
    '[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029'
    '\ud800-\udfff]'
)


def utf8_prefix_length(data: bytes) -> int:
    """`data` 里到最后一个完整 utf-8 字符为止的长度：末尾半个多字节字符不算。拆开解码会在前后
    两次输出里各留一个替换符。"""
    end = len(data)
    for back in range(1, min(4, end) + 1):
        byte = data[end - back]
        if byte & 0xC0 == 0x80:
            continue
        if byte & 0x80 == 0:
            return end
        need = 2 if byte & 0xE0 == 0xC0 else 3 if byte & 0xF0 == 0xE0 else 4
        return end if back >= need else end - back
    return end
