"""`.syndica.yaml` 的 `danger.roles`（M4 D12）：项目配置只能收紧开发期的处置。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.cli.config import load_config
from syndica.cli.policy import RoleTightening, dev_danger_options, dev_table, role_policies
from syndica.kernel import ConfigError, Disposition
from syndica.kernel.danger import RiskLevel

_AGENTS = (
    'agents:\n'
    '  - {name: Kilo, description: k}\n'
    '  - {name: Alpha, description: a}\n'
    '  - {name: Mike, description: m}\n'
)


def _load(tmp_path: Path, danger: str):
    path = tmp_path / '.syndica.yaml'
    path.write_text(f'version: 1\n{_AGENTS}danger:\n{danger}', encoding='utf-8')
    return load_config(path)


def test_each_listed_role_gets_its_own_tightening(tmp_path: Path):
    config = _load(
        tmp_path,
        '  roles:\n'
        '    Mike: {rules: {shell.path_outside_workspace: block}}\n'
        '    Kilo: {policy: block_high_risk}\n',
    )
    assert dict(config.danger_roles) == {
        'Mike': RoleTightening(rules={'shell.path_outside_workspace': Disposition.BLOCK}),
        'Kilo': RoleTightening(block_high_risk=True),
    }


def test_a_config_without_danger_tightens_nothing(tmp_path: Path):
    path = tmp_path / '.syndica.yaml'
    path.write_text(f'version: 1\n{_AGENTS}', encoding='utf-8')
    assert dict(load_config(path).danger_roles) == {}


@pytest.mark.parametrize(
    ('danger', 'anchor', 'fix'),
    [
        (
            '  roles: {Mike: {rules: {shell.path_outside_workspace: allow}}}\n',
            '项目配置只能收紧处置',
            '项目配置做不到',
        ),
        (
            '  roles: {Mike: {rules: {shell.git_state_change: confirm}}}\n',
            '只能比开发期的处置更严',
            '写 block',
        ),
        ('  roles: {Mike: {rules: {shell.nope: block}}}\n', '开发期判定器里的规则 ID', 'shell.'),
        ('  roles: {Nobody: {policy: block_high_risk}}\n', '已载入的角色名', "'Kilo'"),
        ('  roles: {Mike: {policy: allow_all}}\n', '项目配置只能收紧处置', 'allow_all'),
        ('  roles: {Mike: {policy: table}}\n', '项目配置只能收紧处置', 'block_high_risk'),
        ('  roles: {Mike: {}}\n', '至少写 rules 或 policy', 'block_high_risk'),
        ('  roles: {Mike: {rule: {}}}\n', "只含 ['policy', 'rules']", '{rules'),
        ('  role: {}\n', "只含 ['roles']", 'roles'),
    ],
)
def test_anything_that_is_not_a_tightening_is_refused(
    tmp_path: Path, danger: str, anchor: str, fix: str
):
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, danger)
    error = excinfo.value
    assert anchor in error.expected
    assert fix in error.next_action


def test_danger_must_be_a_mapping(tmp_path: Path):
    path = tmp_path / '.syndica.yaml'
    path.write_text(f'version: 1\n{_AGENTS}danger: [roles]\n', encoding='utf-8')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert 'danger 是 Mapping' in excinfo.value.expected


def test_a_rule_tightening_overlays_the_dev_table_for_that_role_only(tmp_path: Path):
    judge, _ = dev_danger_options(tmp_path)
    policies = role_policies(
        judge,
        {'Mike': RoleTightening(rules={'shell.path_outside_workspace': Disposition.BLOCK})},
    )
    assert list(policies) == ['Mike']
    expected = dev_table()
    expected['shell.path_outside_workspace'] = Disposition.BLOCK
    assert dict(policies['Mike'].table) == expected


def test_block_high_risk_blocks_every_high_rule_and_leaves_the_rest(tmp_path: Path):
    judge, _ = dev_danger_options(tmp_path)
    table = dict(role_policies(judge, {'Kilo': RoleTightening(block_high_risk=True)})['Kilo'].table)
    base = dev_table()
    for rule in judge.rules:
        if rule.level is RiskLevel.HIGH:
            assert table[rule.rule_id] is Disposition.BLOCK
        else:
            assert table[rule.rule_id] is base[rule.rule_id]


def test_a_tightening_never_loosens_an_entry(tmp_path: Path):
    judge, _ = dev_danger_options(tmp_path)
    table = role_policies(
        judge,
        {'Mike': RoleTightening(rules={'shell.git_state_change': Disposition.CONFIRM})},
    )['Mike'].table
    assert table['shell.git_state_change'] is Disposition.BLOCK
