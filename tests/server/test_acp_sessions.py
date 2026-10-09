"""ACP sessionId 落在哪个 Syndica 会话上：换一个索引对象照样取得回。

名字与内容只认 32 位小写十六进制。
"""

from __future__ import annotations

from pathlib import Path

from syndica.kernel import RuleRedactor
from syndica.serve import _AcpSessions  # pyright: ignore[reportPrivateUsage]

_ACP = '0f3c9a1e5b7d2c4e6a8b0d1f3e5c7a9b'
_SID = '9b7a5c3e1f0d8b6a4e2c0d7b5a3e1c9f'


def test_a_remembered_session_is_recalled_by_a_fresh_index(tmp_path: Path) -> None:
    root = tmp_path / 'acp-sessions'
    _AcpSessions(root, RuleRedactor()).remember(_ACP, _SID)
    assert _AcpSessions(root, RuleRedactor()).recall(_ACP) == _SID
    assert _AcpSessions(root, RuleRedactor()).recall(_SID) is None


def test_a_session_id_that_is_not_hex_never_becomes_a_path(tmp_path: Path) -> None:
    root = tmp_path / 'acp-sessions'
    root.mkdir()
    (tmp_path / 'planted').write_text(_SID, encoding='utf-8')
    (tmp_path / 'victim').write_text('original', encoding='utf-8')
    index = _AcpSessions(root, RuleRedactor())
    assert index.recall('../planted') is None
    index.remember('../victim', _SID)
    assert (tmp_path / 'victim').read_text(encoding='utf-8') == 'original'
    assert list(root.iterdir()) == []


def test_a_stored_value_that_is_not_a_session_id_is_not_recalled(tmp_path: Path) -> None:
    root = tmp_path / 'acp-sessions'
    root.mkdir()
    (root / _ACP).write_text('../../elsewhere', encoding='utf-8')
    assert _AcpSessions(root, RuleRedactor()).recall(_ACP) is None
