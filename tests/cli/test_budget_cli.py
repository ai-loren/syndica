"""CLI 的预算（spec §8.4，计划 P1 H2 / M12 / M13）：`--max-*` 管整棵派活树、`prices:` 排在 backend
默认价格之前、预算那一行只在人设了预算或真停在预算上时打、回放不了的预算不写回放文档。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from syndica.backends.llm_openai import FreeTierPrices
from syndica.cli.config import config_from_text
from syndica.cli.host import (
    RuntimeCliHost,
    _unreplayable_budget,  # pyright: ignore[reportPrivateUsage]
    _with_stored_limits,  # pyright: ignore[reportPrivateUsage]
)
from syndica.cli.main import (
    CliIO,
    RunRequest,
    _parser,  # pyright: ignore[reportPrivateUsage]
    _ParserExit,  # pyright: ignore[reportPrivateUsage]
)
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    ConfigError,
    ContextBlock,
    FileSessionStore,
    LLMConfig,
    ScriptedLLM,
    ScriptedTurn,
    SessionBudget,
    ToolCall,
)
from syndica.kernel.budget import Budget, BudgetPolicy, Prices, Usage
from syndica.kernel.budget_tree import BudgetRefused


def _config(text: str):
    return config_from_text(Path('.syndica.yaml'), 'version: 1\n' + text)


def _host(
    tmp_path: Path,
    llm: ScriptedLLM,
    *,
    stderr: StringIO,
    stdin: str = '',
    stdout: StringIO | None = None,
) -> RuntimeCliHost:
    return RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(stdin), stdout=stdout or StringIO(), stderr=stderr),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
        prices=FreeTierPrices(),
    )


def _tool(call_id: str) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name='Glob', arguments='{"pattern": "*"}')]
    )


# ---- prices: ----


def test_prices_are_read_as_decimals_and_cached_input_defaults_to_input() -> None:
    config = _config(
        'prices:\n'
        '  m: {input: 0.5, output: 2}\n'
        "  'openrouter:n': {input: 1, output: 1, cached_input: 0.1}\n"
    )
    assert config.prices['m'] == Prices(
        input=Decimal('0.5'), output=Decimal('2'), cached_input=Decimal('0.5')
    )
    assert config.prices['openrouter:n'].cached_input == Decimal('0.1')


@pytest.mark.parametrize(
    ('entry', 'anchor'),
    [
        ('{input: yes, output: 1}', 'yes / no'),
        ('{input: -1, output: 1}', '>= 0'),
        ('{input: .nan, output: 1}', '>= 0'),
        ('{input: 1}', "缺 ['output']"),
        ('{input: 1, output: 1, cache_write: 2}', "多出 ['cache_write']"),
        ('{input: one, output: 1}', '写一个数字'),
        ('[1, 2]', 'input, output, cached_input'),
    ],
)
def test_a_malformed_price_is_refused_naming_the_field(entry: str, anchor: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _config(f'prices:\n  m: {entry}\n')
    assert anchor in str(caught.value)


# ---- 命令行 ----


def test_max_cost_is_parsed_as_an_exact_decimal() -> None:
    namespace = _parser().parse_args(
        ['run', 'go', '--max-cost', '0.1', '--on-exhausted', 'hard_stop']
    )
    assert namespace.max_cost == Decimal('0.1')
    assert namespace.on_exhausted == 'hard_stop'


@pytest.mark.parametrize('value', ['nan', '0', '-1', 'abc', 'inf'])
def test_a_max_cost_that_is_not_a_positive_amount_is_refused(value: str) -> None:
    with pytest.raises(_ParserExit) as caught:
        _parser().parse_args(['run', 'go', '--max-cost', value])
    assert '--max-cost' in str(caught.value)


def test_an_unknown_exhaustion_disposition_is_refused() -> None:
    with pytest.raises(_ParserExit) as caught:
        _parser().parse_args(['chat', '--on-exhausted', 'panic'])
    assert 'ask_human' in str(caught.value)


# ---- 预算那一行 ----


def test_resuming_without_a_new_limit_keeps_the_stored_one_and_its_usage(tmp_path: Path) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='one'), ScriptedTurn(content='two')])
    host = _host(tmp_path, llm, stderr=StringIO())
    try:
        host.run_request(RunRequest(prompt='go', agent='Worker', max_turns=3))
        store = FileSessionStore(tmp_path / '.syndica' / 'sessions')
        [meta] = asyncio.run(store.list_metas())
        host.run_request(RunRequest(prompt='again', session=meta.sid, allow_resume=True))
    finally:
        host.close()
    record = asyncio.run(store.load(meta.sid))
    assert record is not None and record.budget is not None
    assert (record.budget.turns, record.budget.used.turns) == (3, 2)


def test_a_new_limit_on_resume_replaces_the_stored_one() -> None:
    stored = SessionBudget(tokens=50, cost=Decimal('1'), turns=3, used=Usage(turns=2))
    policy = BudgetPolicy(root=Budget(turns=200))
    kept = _with_stored_limits(policy, stored, turns=None, tokens=None, cost=None)
    given = _with_stored_limits(
        BudgetPolicy(root=Budget(turns=9, tokens=70)), stored, turns=9, tokens=70, cost=None
    )
    assert (kept.root.turns, kept.root.tokens, kept.root.cost) == (3, 50, Decimal('1'))
    assert (given.root.turns, given.root.tokens, given.root.cost) == (9, 70, Decimal('1'))


def test_verdict_exit_without_acceptance_is_exit_2(tmp_path: Path) -> None:
    host = _host(tmp_path, ScriptedLLM([ScriptedTurn(content='done')]), stderr=StringIO())
    try:
        with pytest.raises(ConfigError) as caught:
            host.run_request(RunRequest(prompt='go', verdict_exit=True))
    finally:
        host.close()
    assert getattr(caught.value, 'exit_code', None) == 2
    assert '--verdict-exit' in caught.value.next_action


def test_a_run_without_a_budget_of_its_own_prints_no_budget_line(tmp_path: Path) -> None:
    stderr = StringIO()
    host = _host(tmp_path, ScriptedLLM([ScriptedTurn(content='done')]), stderr=stderr)
    try:
        host.run_request(RunRequest(prompt='go', agent='Worker'))
    finally:
        host.close()
    assert '预算：' not in stderr.getvalue()


def test_a_run_with_max_turns_prints_what_it_used(tmp_path: Path) -> None:
    stderr = StringIO()
    llm = ScriptedLLM([_tool('g1'), ScriptedTurn(content='done')])
    host = _host(tmp_path, llm, stderr=stderr)
    try:
        output = host.run_request(RunRequest(prompt='go', agent='Worker', max_turns=5))
    finally:
        host.close()
    assert output == 'done'
    [line] = [text for text in stderr.getvalue().splitlines() if text.startswith('预算：')]
    assert line.startswith('预算：轮次 2 / 5；') and line.endswith('停止原因 completed')


# ---- 成本 ----


def test_a_cost_cap_on_a_free_model_runs_and_says_where_prices_come_from(tmp_path: Path) -> None:
    stderr = StringIO()
    llm = ScriptedLLM(
        [ScriptedTurn(content='done')],
        config=LLMConfig(provider='scripted', model='vendor/model:free'),
    )
    host = _host(tmp_path, llm, stderr=stderr)
    try:
        output = host.run_request(RunRequest(prompt='go', agent='Worker', max_cost=Decimal('0.01')))
    finally:
        host.close()
    assert output == 'done'
    assert '成本上限 0.01' in stderr.getvalue() and 'prices:' in stderr.getvalue()
    assert '成本 0 / 0.01' in stderr.getvalue()


def test_a_cost_cap_on_a_model_without_a_price_is_refused_before_it_runs(tmp_path: Path) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='never')])
    host = _host(tmp_path, llm, stderr=StringIO())
    try:
        with pytest.raises(BudgetRefused) as caught:
            host.run_request(RunRequest(prompt='go', agent='Worker', max_cost=Decimal('1')))
    finally:
        host.close()
    assert 'prices' in caught.value.next_action
    assert llm.calls == []


# ---- ask_human ----


def test_ask_human_reads_how_many_more_turns_from_stdin(tmp_path: Path) -> None:
    stdout = StringIO()
    llm = ScriptedLLM([_tool('g1'), _tool('g2'), _tool('g3'), ScriptedTurn(content='done')])
    host = _host(tmp_path, llm, stderr=StringIO(), stdin='2\n', stdout=stdout)
    try:
        output = host.run_request(
            RunRequest(prompt='go', agent='Worker', max_turns=2, on_exhausted='ask_human')
        )
    finally:
        host.close()
    assert output == 'done'
    assert len(llm.calls) == 4
    assert '再给它几轮' in stdout.getvalue()


def test_ask_human_with_no_answer_wraps_up(tmp_path: Path) -> None:
    llm = ScriptedLLM([_tool('g1'), ScriptedTurn(content='wrapped')])
    host = _host(tmp_path, llm, stderr=StringIO(), stdin='')
    try:
        output = host.run_request(
            RunRequest(prompt='go', agent='Worker', max_turns=2, on_exhausted='ask_human')
        )
    finally:
        host.close()
    assert output == 'wrapped'
    assert len(llm.calls) == 2


# ---- 回放 ----


@pytest.mark.parametrize(
    ('request_', 'anchor'),
    [
        (RunRequest(prompt='go', max_cost=Decimal('1')), '--max-cost'),
        (RunRequest(prompt='go', max_wall_clock=60.0), '--max-wall-clock'),
        (RunRequest(prompt='go', on_exhausted='ask_human'), 'ask_human'),
    ],
)
def test_budgets_replay_cannot_reproduce_skip_the_replay_document(
    request_: RunRequest, anchor: str
) -> None:
    reason = _unreplayable_budget(request_)
    assert reason is not None and anchor in reason


@pytest.mark.parametrize(
    'request_',
    [
        RunRequest(prompt='go'),
        RunRequest(prompt='go', max_turns=3, max_tokens=1000, on_exhausted='hard_stop'),
    ],
)
def test_turn_and_token_budgets_stay_replayable(request_: RunRequest) -> None:
    assert _unreplayable_budget(request_) is None


def test_the_wrap_up_block_is_kernel_text_not_a_model_reply(tmp_path: Path) -> None:
    llm = ScriptedLLM([_tool('g1'), ScriptedTurn(content='wrapped')])
    host = _host(tmp_path, llm, stderr=StringIO())
    try:
        host.run_request(RunRequest(prompt='go', agent='Worker', max_turns=2))
    finally:
        host.close()
    wrap = [
        block
        for block in llm.calls[1]
        if isinstance(block, ContextBlock) and '最后一轮' in str(block.content)
    ]
    assert len(wrap) == 1 and wrap[0].role == 'user'
