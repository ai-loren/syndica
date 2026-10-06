"""截断不在凭证中间下刀（spec §12.3）：交给模型、写进轨迹的视图先截后脱敏时，跨过刀口的凭证
一个字符也不能留下，否则脱敏器认不出那一截、它原样落进 trace 与会话。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from syndica.kernel import local_pty, local_workspace
from syndica.kernel.acceptance import Verdict, VerdictStatus
from syndica.kernel.agent import _render_feedback  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.artifacts import GenericHandler
from syndica.kernel.clipping import clip_head, clip_tail, drop_cut_end, drop_cut_start
from syndica.kernel.compaction import _truncated  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.context import Context, ContextBlock, ToolCall
from syndica.kernel.drift import call_lines
from syndica.kernel.long_horizon import MESSAGE_LIMIT
from syndica.kernel.long_horizon_module import _clipped  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.tools.local import (
    _clipped as _clipped_output,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.tools.local import _listed  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.verifiers import render_transcript

_SECRETS = (
    'sk-proj-' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0',
    'ghp_' + 'Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0FeDcBa98',
    'AKIA' + 'QWERTYUIOPASDFGH',
)
_FRAGMENT = 8


def _leaked(shown: str, secret: str) -> str:
    redacted = RuleRedactor().redact(shown)
    for start in range(len(secret) - _FRAGMENT + 1):
        if secret[start : start + _FRAGMENT] in redacted:
            return secret[start : start + _FRAGMENT]
    return ''


def _sweep(view: Callable[[str], str], *, width: int, trail: int | None = None) -> None:
    """凭证放在每一个偏移上，使刀口依次落在它前面、中间每一个字符与后面。"""
    for secret in _SECRETS:
        for offset in range(width - len(secret) - 2, width + 2):
            text = 'y' * offset + f' --key={secret} done ' + 'z' * (trail or width)
            assert _leaked(view(text), secret) == '', (secret, offset)


def test_clip_head_drops_the_credential_run_the_cut_lands_in() -> None:
    assert clip_head('alpha sk-secret1234 omega', 12) == 'alpha '
    assert clip_head('alpha sk-secret1234 omega', 19) == 'alpha sk-secret1234'
    assert clip_head('alpha sk-secret1234 omega', 20) == 'alpha sk-secret1234 '


def test_clip_tail_drops_the_credential_run_the_cut_lands_in() -> None:
    assert clip_tail('alpha sk-secret1234 omega', 15) == ' omega'
    assert clip_tail('alpha sk-secret1234 omega', 19) == 'sk-secret1234 omega'


def test_text_that_cannot_hold_a_credential_is_cut_exactly() -> None:
    chinese = '这是一段没有空格的中文正文用来验证刀口不会把整段中文丢掉'
    assert clip_head(chinese, 10) == chinese[:10]
    assert clip_tail(chinese, 10) == chinese[-10:]
    assert clip_head('{"a":"b","c":"d"}', 9) == '{"a":"b",'


def test_text_within_the_limit_comes_back_unchanged() -> None:
    assert clip_head('sk-secret1234', 13) == 'sk-secret1234'
    assert clip_tail('sk-secret1234', 13) == 'sk-secret1234'


def test_an_unseen_neighbour_counts_as_part_of_a_credential() -> None:
    assert drop_cut_end('done sk-secre') == 'done '
    assert drop_cut_start('t1234 done') == ' done'
    assert drop_cut_end('done 完') == 'done 完'
    assert drop_cut_start('完 done') == '完 done'


def test_the_drift_judge_view_never_keeps_part_of_a_credential() -> None:
    def view(text: str) -> str:
        block = ContextBlock(
            role='assistant',
            tool_calls=[ToolCall(id='c', name='Bash', arguments=json.dumps({'command': text}))],
        )
        return call_lines([block], line_chars=300)[0]

    _sweep(view, width=300)


def test_the_acceptance_transcript_never_keeps_part_of_a_credential() -> None:
    def arguments(text: str) -> str:
        ctx = Context()
        ctx.append(
            ContextBlock(
                role='assistant',
                tool_calls=[ToolCall(id='c', name='Bash', arguments=json.dumps({'command': text}))],
            )
        )
        return render_transcript(ctx)

    def reply(text: str) -> str:
        ctx = Context()
        ctx.append(ContextBlock(role='assistant', content=text))
        return render_transcript(ctx)

    _sweep(arguments, width=200)
    _sweep(reply, width=2000)


@pytest.mark.parametrize('limit', [400, 2000])
def test_command_output_head_and_tail_never_keep_part_of_a_credential(limit: int) -> None:
    head = int(limit * 0.2)
    _sweep(lambda text: _clipped_output(text, limit)[0], width=head, trail=limit * 3)
    for secret in _SECRETS:
        for offset in range(limit - head - len(secret) - 2, limit - head + 2):
            text = 'a' * (limit * 3) + f' --key={secret} ' + 'b' * offset
            assert _leaked(_clipped_output(text, limit)[0], secret) == '', (secret, offset)


def test_a_clipped_search_result_never_keeps_part_of_a_credential() -> None:
    _sweep(lambda text: _listed([text], 300, 'narrow')[0], width=300)


def test_compaction_and_strategy_text_never_keep_part_of_a_credential() -> None:
    _sweep(
        lambda text: str(_truncated(ContextBlock(role='tool', content=text), 500).content),
        width=500,
    )
    _sweep(_clipped, width=MESSAGE_LIMIT)


def test_feedback_reason_never_keeps_part_of_a_credential() -> None:
    def view(text: str) -> str:
        verdict = Verdict(
            status=VerdictStatus.UNVERIFIED,
            outcomes=(),
            needs_human=(),
            reason=text,
            unverified_cause='verifier_timeout',
        )
        return _render_feedback(verdict)

    _sweep(view, width=500)


def test_an_artifact_preview_never_keeps_part_of_a_credential() -> None:
    class _Reader:
        def __init__(self, text: str) -> None:
            self.text = text

        async def read(self, ref: object) -> str:
            return self.text

    def view(text: str) -> str:
        preview = asyncio.run(GenericHandler().preview(cast(Any, None), cast(Any, _Reader(text))))
        return preview.text

    _sweep(view, width=2000)


def test_dropped_command_output_never_keeps_part_of_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_workspace, '_CAPTURE_HEAD', 300)
    monkeypatch.setattr(local_workspace, '_CAPTURE_TAIL', 300)

    def view(text: str) -> str:
        capture = local_workspace._Capture()  # pyright: ignore[reportPrivateUsage]
        capture.extend(text.encode())
        return capture.rendered(settled=True)[0]

    _sweep(view, width=300)
    for secret in _SECRETS:
        for offset in range(300 - len(secret) - 2, 302):
            text = 'a' * 900 + f' --key={secret} ' + 'b' * offset
            assert _leaked(view(text), secret) == '', (secret, offset)


def test_dropped_terminal_output_never_keeps_part_of_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_pty, '_KEEP_HEAD', 300)
    monkeypatch.setattr(local_pty, '_KEEP_TAIL', 300)

    def view(text: str) -> str:
        session = local_pty._Session(  # pyright: ignore[reportPrivateUsage]
            process=cast(Any, None), master=-1, cols=80, rows=24, owner=None
        )
        session.extend(text.encode())
        return session.since(0, settled=True)[0]

    _sweep(view, width=300)
    for secret in _SECRETS:
        for offset in range(300 - len(secret) - 2, 302):
            text = 'a' * 900 + f' --key={secret} ' + 'b' * offset
            assert _leaked(view(text), secret) == '', (secret, offset)
