"""ACP 外部进程的 stderr：整行脱敏之后才截，跨过上限的凭证不留半截；单行缓冲有硬上限。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from syndica.backends.acp.process import (
    _LINE_CAP,  # pyright: ignore[reportPrivateUsage]
    ExternalProcess,
    _StderrLines,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel import RuleRedactor

_STRADDLER = 'sk-live-QuizWaltzVex9876JUMP'
_LOGGER = 'syndica.backends.acp.process'


def _fragments(secret: str, size: int = 6) -> list[str]:
    return [secret[start : start + size] for start in range(len(secret) - size + 1)]


def _stderr_texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    texts: list[str] = []
    for record in caplog.records:
        if record.levelno == logging.INFO and 'stderr' in str(record.msg):
            assert isinstance(record.args, tuple)
            texts.append(str(record.args[1]))
    return texts


def _warning_args(caplog: pytest.LogCaptureFixture) -> list[tuple[object, ...]]:
    found: list[tuple[object, ...]] = []
    for record in caplog.records:
        if record.levelno == logging.WARNING and isinstance(record.args, tuple):
            found.append(record.args)
    return found


async def _log_stderr_of(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: str, needle: str, limit: int
) -> None:
    script = '\n'.join(
        [
            'import sys, time',
            f'sys.stderr.write({payload!r})',
            'sys.stderr.flush()',
            'time.sleep(30)',
        ]
    )
    process = ExternalProcess(
        [sys.executable, '-c', script],
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
        cwd=str(tmp_path),
        redactor=RuleRedactor(secrets=[_STRADDLER]),
        grace=0.05,
        stderr_limit=limit,
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await process.start()
        try:
            deadline = time.monotonic() + 5
            while needle not in caplog.text and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            assert needle in caplog.text
        finally:
            await process.aclose()


async def test_a_secret_straddling_the_stderr_limit_leaves_no_fragment_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    payload = 'x' * 50 + _STRADDLER + '\nafter\n'
    await _log_stderr_of(tmp_path, caplog, payload, '不超过', limit=64)
    assert not [piece for piece in _fragments(_STRADDLER) if piece in caplog.text]
    texts = _stderr_texts(caplog)
    assert texts[0].startswith('x' * 50)
    assert sum(len(text.encode('utf-8')) for text in texts) <= 64
    assert [args[1] for args in _warning_args(caplog)] == [64]


async def test_a_secret_straddling_the_line_cap_leaves_no_fragment_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    head = 'word ' * (_LINE_CAP // 5 - 2)
    assert len(head) < _LINE_CAP < len(head) + len(_STRADDLER)
    payload = head + _STRADDLER + 'y' * 1000 + '\ntail line\n'
    await _log_stderr_of(tmp_path, caplog, payload, 'tail line', limit=1_048_576)
    assert not [piece for piece in _fragments(_STRADDLER) if piece in caplog.text]
    texts = _stderr_texts(caplog)
    assert texts[0].startswith('word word')
    assert texts[-1] == 'tail line'
    assert [args[1] for args in _warning_args(caplog)] == [_LINE_CAP]


def test_an_endless_line_holds_at_most_the_line_cap_in_memory() -> None:
    emitted: list[str] = []
    lines = _StderrLines(
        RuleRedactor(),
        64 * 1024 * 1024,
        emit=emitted.append,
        capped=lambda: None,
        clipped=lambda cap: None,
    )
    chunk = b'x' * 4096
    tracemalloc.start()
    try:
        for _ in range(4096):
            lines.feed(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * _LINE_CAP


def test_a_line_that_fits_exactly_is_not_reported_as_dropped() -> None:
    emitted: list[str] = []
    capped: list[bool] = []
    lines = _StderrLines(
        RuleRedactor(),
        5,
        emit=emitted.append,
        capped=lambda: capped.append(True),
        clipped=lambda cap: None,
    )
    lines.feed(b'hello\n')
    lines.close()
    assert emitted == ['hello']
    assert capped == []
    lines.feed(b'more\n')
    assert emitted == ['hello']
    assert capped == [True]
