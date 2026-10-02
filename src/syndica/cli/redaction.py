"""从宿主环境里挑出要逐字换掉的密钥值，交给内核的 `RuleRedactor`（spec D12、M3 D4）。

规则本身与「名字像不像密钥」在内核（`syndica.kernel.redaction`）；这里只做内核不能做的两件事：读宿主
的环境，以及查一个值是不是磁盘上存在的路径。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from syndica.cli.export import HEADER_VARIABLES, header_values
from syndica.kernel.redaction import RedactionRule, RuleRedactor, looks_secret

__all__ = ['host_redactor', 'host_secrets']

# 值短于这个长度不当凭证：`PASS=on` 这种开关按值替换会把正文里每个 `on` 都换掉。
_MIN_VALUE = 8


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


def host_secrets(environ: Mapping[str, str]) -> tuple[str, ...]:
    """名字像密钥、值像凭证的环境变量值。经 `echo | base64`、Kubernetes secret 传进来的值常带着
    换行，正文里出现的是去掉它的那一份：两份都交。OTLP 导出的头（多半是鉴权）不论名字都交。"""
    values: set[str] = set()
    for name, value in environ.items():
        if looks_secret(name):
            values.update(v for v in {value, value.strip()} if _credential_like(v))
    for variable in HEADER_VARIABLES:
        exported = header_values(environ.get(variable, ''))
        values.update(v for v in exported if _credential_like(v))
    return tuple(sorted(values))


def host_redactor(environ: Mapping[str, str], rules: Sequence[RedactionRule] = ()) -> RuleRedactor:
    """宿主落盘与打印用的脱敏器：环境里的密钥值，加 `.syndica.yaml` 的用户规则。"""
    return RuleRedactor(secrets=host_secrets(environ), rules=rules)
