"""零 LLM 词面单位：目标导向型压缩与漂移锚点检查共用的那把尺子。"""

from __future__ import annotations

from syndica.kernel.lexical import overlap, terms


def test_latin_words_are_lowercased_and_short_or_pointless_words_dropped() -> None:
    assert terms('The Invoice is OK for Postgres file') == {'invoice', 'postgres'}


def test_paths_and_identifiers_split_on_separators() -> None:
    assert terms('cat schema/ledger.sql --max_turns=3') == {
        'cat',
        'schema',
        'ledger',
        'sql',
        'max_turns',
    }


def test_han_runs_become_adjacent_pairs_and_a_lone_character_stays_whole() -> None:
    assert terms('发票编号，的') == {'发票', '票编', '编号', '的'}


def test_mixed_scripts_contribute_both_kinds_of_unit() -> None:
    assert terms('迁移 billing 账本') == {'迁移', 'billing', '账本'}


def test_overlap_counts_shared_units_once() -> None:
    vocabulary = terms('invoice numbering 单调递增')
    assert overlap('invoice invoice 递增 numbering', vocabulary) == 3
    assert overlap('nothing shared', vocabulary) == 0
