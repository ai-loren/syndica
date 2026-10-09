"""一份 jsonl 任务清单。一条失败不中断其余。

堆栈进日志。交回调用方的文本只有任务 id、退出码与异常类型名，不带异常消息：消息里可能有密钥。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from syndica.kernel import ConfigError

__all__ = ['BatchResult', 'BatchTask', 'parse_dataset', 'run_batch', 'run_dataset']

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchTask:
    id: str
    prompt: str


@dataclass(frozen=True)
class BatchResult:
    """`error` 是 `exit <码>` 或异常类型名；成功时为 None。"""

    id: str
    error: str | None = None


def parse_dataset(text: str) -> tuple[list[BatchTask], list[tuple[int, str]]]:
    """能解析的行成为任务。坏行记进第二项（行号, 期望），不丢掉后面的行。"""
    tasks: list[BatchTask] = []
    errors: list[tuple[int, str]] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            task = _task(line, number)
            if task.id in seen:
                raise ConfigError(
                    expected='每个任务 id 只出现一次',
                    received=f'第 {number} 行的 id {task.id!r} 前面已经有了',
                    next_action='给这一行换一个 id；结果按 id 报，重复就分不清是哪一条',
                )
        except ConfigError as exc:
            errors.append((number, exc.expected))
            continue
        except Exception as exc:
            logger.exception('batch line %s failed', number)
            errors.append((number, type(exc).__name__))
            continue
        seen.add(task.id)
        tasks.append(task)
    return tasks, errors


def run_batch(tasks: list[BatchTask], run_one: Callable[[BatchTask], int]) -> list[BatchResult]:
    """按清单顺序跑。`run_one` 交回这次 run 的退出码；非零或抛出都记成失败，接着跑下一条。"""
    results: list[BatchResult] = []
    for task in tasks:
        try:
            code = run_one(task)
        except Exception as exc:
            logger.exception('batch task %s failed', task.id)
            results.append(BatchResult(task.id, type(exc).__name__))
            continue
        results.append(BatchResult(task.id, None if code == 0 else f'exit {code}'))
    return results


def run_dataset(text: str, run_one: Callable[[BatchTask], int]) -> tuple[str, int]:
    """跑完整份清单。stdout 是一行一个 JSON；有失败时退出码是 1。"""
    tasks, errors = parse_dataset(text)
    results = run_batch(tasks, run_one)
    rows: list[dict[str, object]] = [
        {'line': number, 'ok': False, 'error': expected} for number, expected in errors
    ]
    for result in results:
        row: dict[str, object] = {'id': result.id, 'ok': result.error is None}
        if result.error is not None:
            row['error'] = result.error
        rows.append(row)
    body = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
    failed = bool(errors) or any(result.error is not None for result in results)
    return body, 1 if failed else 0


def _task(line: str, number: int) -> BatchTask:
    try:
        payload = cast(object, json.loads(line))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            expected='每一行是一个 JSON 对象，含字符串 id 与 prompt',
            received=f'第 {number} 行不是 JSON',
            next_action='改成 {"id": "...", "prompt": "..."}',
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigError(
            expected='每一行是一个 JSON 对象，含字符串 id 与 prompt',
            received=f'第 {number} 行是 {type(payload).__name__}',
            next_action='改成 {"id": "...", "prompt": "..."}',
        )
    row = cast(dict[str, object], payload)
    ident = row.get('id')
    prompt = row.get('prompt')
    if isinstance(ident, str) and ident and isinstance(prompt, str) and prompt:
        return BatchTask(ident, prompt)
    raise ConfigError(
        expected='id 与 prompt 都是非空字符串',
        received=f'第 {number} 行的 id={ident!r}，prompt 的类型是 {type(prompt).__name__}',
        next_action='补上字符串 id 与 prompt',
    )
