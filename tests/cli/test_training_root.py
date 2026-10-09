"""组合根的 `batch`：一次 run 以非零码结束就记成失败；配置、角色与目标原样交给每一次 run。"""

from __future__ import annotations

import json
from pathlib import Path

from syndica.__main__ import _Training  # pyright: ignore[reportPrivateUsage]
from syndica.cli.main import CliOutcome, RunRequest


class _Host:
    def __init__(self) -> None:
        self.requests: list[RunRequest] = []

    def run_request(self, request: RunRequest) -> str | CliOutcome:
        self.requests.append(request)
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
    assert [request.prompt for request in host.requests] == ['breaks', 'fine']
    assert outcome.exit_code == 1
    rows = [json.loads(line) for line in outcome.output.splitlines()]
    assert rows == [{'id': 'a', 'ok': False, 'error': 'exit 1'}, {'id': 'b', 'ok': True}]


def test_the_config_agent_and_goal_reach_each_run(tmp_path: Path) -> None:
    dataset = tmp_path / 'tasks.jsonl'
    dataset.write_text(
        '{"id": "a", "prompt": "one", "agent": "Worker", "goal": "ship it"}\n'
        '{"id": "b", "prompt": "two"}\n',
        encoding='utf-8',
    )
    host = _Host()
    _Training(host).batch(str(dataset), 'custom.yaml')  # pyright: ignore[reportArgumentType]
    assert [(r.config, r.agent, r.goal) for r in host.requests] == [
        ('custom.yaml', 'Worker', 'ship it'),
        ('custom.yaml', None, None),
    ]


def test_a_misspelled_key_is_a_bad_line_not_a_silent_default(tmp_path: Path) -> None:
    dataset = tmp_path / 'tasks.jsonl'
    dataset.write_text('{"id": "a", "prompt": "one", "agnet": "Worker"}\n', encoding='utf-8')
    host = _Host()
    outcome = _Training(host).batch(str(dataset), '.syndica.yaml')  # pyright: ignore[reportArgumentType]
    assert host.requests == []
    assert outcome.exit_code == 1
    [row] = [json.loads(line) for line in outcome.output.splitlines()]
    assert row['line'] == 1 and row['ok'] is False
