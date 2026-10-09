"""组合根的 `batch`：一次 run 以非零码结束就记成失败，不因为它没抛异常而算成功。"""

from __future__ import annotations

import json
from pathlib import Path

from syndica.__main__ import _Training  # pyright: ignore[reportPrivateUsage]
from syndica.cli.main import CliOutcome, RunRequest


class _Host:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run_request(self, request: RunRequest) -> str | CliOutcome:
        self.prompts.append(request.prompt)
        if request.prompt == 'breaks':
            return CliOutcome('期望: …\n实际: …\n下一步: …', 1)
        return 'done'


def test_a_run_that_exits_nonzero_is_a_failed_row_and_the_next_task_still_runs(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / 'tasks.jsonl'
    dataset.write_text(
        '{"id": "a", "prompt": "breaks"}\n{"id": "b", "prompt": "fine"}\n', encoding='utf-8'
    )
    host = _Host()
    outcome = _Training(host).batch(str(dataset), '.syndica.yaml')  # pyright: ignore[reportArgumentType]
    assert host.prompts == ['breaks', 'fine']
    assert outcome.exit_code == 1
    rows = [json.loads(line) for line in outcome.output.splitlines()]
    assert rows == [{'id': 'a', 'ok': False, 'error': 'exit 1'}, {'id': 'b', 'ok': True}]
