"""`syndica batch` / `syndica export` 交给注入的训练出口，不在 CLI 里 import runner。"""

from __future__ import annotations

from io import StringIO

from syndica.cli.main import CliIO, CliOutcome, main


class _Training:
    def __init__(self) -> None:
        self.batch_calls: list[tuple[str, str]] = []
        self.export_calls: list[tuple[str, str, str, bool]] = []

    def batch(self, dataset: str, config: str) -> CliOutcome:
        self.batch_calls.append((dataset, config))
        return CliOutcome('{"id": "a", "ok": true}\n', 0)

    def export(
        self,
        fmt: str,
        *,
        config: str,
        output: str,
        include_unverified: bool,
    ) -> CliOutcome:
        self.export_calls.append((fmt, config, output, include_unverified))
        return CliOutcome('wrote 1\n', 0)


def test_batch_and_export_reach_the_injected_training_control() -> None:
    training = _Training()
    stdout = StringIO()
    code = main(
        ['batch', 'tasks.jsonl'],
        io=CliIO(StringIO(), stdout, StringIO()),
        training=training,
    )
    assert code == 0
    assert training.batch_calls == [('tasks.jsonl', '.syndica.yaml')]
    assert '"ok": true' in stdout.getvalue()

    exported = StringIO()
    code = main(
        ['export', 'sft', '--output', 'out', '--include-unverified'],
        io=CliIO(StringIO(), exported, StringIO()),
        training=training,
    )
    assert code == 0
    assert training.export_calls == [('sft', '.syndica.yaml', 'out', True)]


def test_batch_without_training_says_how_to_start() -> None:
    stderr = StringIO()
    code = main(['batch', 'tasks.jsonl'], io=CliIO(StringIO(), StringIO(), stderr))
    assert code == 2
    assert 'training.batch' in stderr.getvalue()
