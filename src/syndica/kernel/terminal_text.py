"""会在终端上改写显示的字符。内核按它拒收第三方清单里要拿去给人看的字符串（Module 的说明、安全
说明、版本）；CLI 进终端之前按逐字相同的一份转义（`cli/terminal.py`，它不能为此加载内核）。"""

from __future__ import annotations

import re

__all__ = ['UNSAFE_TEXT']

# 除换行与制表符外的 C0、DEL、C1、双向文字控制字符、U+2028 / U+2029，以及落单的代理字符：被
# `max_tokens` 截断的 emoji 就是一个落单的高位代理，它编码成 UTF-8 时抛错。
UNSAFE_TEXT = re.compile(
    '[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029'
    '\ud800-\udfff]'
)
