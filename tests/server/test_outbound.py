"""外发出口：每个字符串叶子先脱敏、再截断。"""

from __future__ import annotations

import json

from syndica.kernel import RuleRedactor
from syndica.server.outbound import FIELD_LIMIT, encoded, json_response, redacted, sse_frame
from syndica.server.streams import PING, RESET, Entry

_SECRET = 'sekrit-value-123456'
_REDACTOR = RuleRedactor(secrets=(_SECRET,))


def test_every_string_leaf_is_redacted_and_keys_are_kept():
    value = {
        'a': [f'x {_SECRET}', {'b': _SECRET}],
        't': (_SECRET, 'plain'),
        'n': 3,
        'none': None,
        'flag': True,
    }
    assert redacted(value, _REDACTOR) == {
        'a': ['x [REDACTED:secret.env_value]', {'b': '[REDACTED:secret.env_value]'}],
        't': ['[REDACTED:secret.env_value]', 'plain'],
        'n': 3,
        'none': None,
        'flag': True,
    }


# 含 `=` 的密钥：截断在凭证字符之外的地方下刀，`=` 之后就是一刀。先截后脱敏的话，`=` 之前的那半截
# 留在输出里，脱敏器已经认不出它。
_SPLITTABLE = 'hunter2-alpha=hunter2-bravo-charlie'


def test_a_secret_across_the_cut_is_redacted_before_the_field_is_clipped():
    redactor = RuleRedactor(secrets=(_SPLITTABLE,))
    cut = _SPLITTABLE.index('=') + 1
    text = 'x' * (FIELD_LIMIT - cut) + ' ' + _SPLITTABLE
    out = redacted(text, redactor)
    assert len(out) <= FIELD_LIMIT
    assert not any(_SPLITTABLE[start : start + 4] in out for start in range(len(_SPLITTABLE) - 3))


def test_each_exit_redacts():
    assert _SECRET not in encoded({'k': _SECRET}, _REDACTOR)
    assert _SECRET.encode() not in bytes(json_response({'k': _SECRET}, _REDACTOR).body)
    frame = sse_frame('e1', Entry(4, 'turn_start', {'k': _SECRET}), _REDACTOR)
    assert frame.startswith('id: e1:4\nevent: turn_start\ndata: ')
    assert _SECRET not in frame
    assert json.loads(frame.split('data: ', 1)[1]) == {'k': '[REDACTED:secret.env_value]'}


def test_a_reset_has_no_id_and_a_ping_is_a_comment():
    assert sse_frame('e1', RESET, _REDACTOR) == 'event: reset\ndata: {}\n\n'
    assert sse_frame('e1', PING, _REDACTOR) == ': ping\n\n'
