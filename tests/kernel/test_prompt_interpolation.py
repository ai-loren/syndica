"""System Prompt 插值：白名单 {os}/{platform}/{shell}，禁止 {cwd}，freeze 时插一次。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from syndica.kernel.catalog import AgentCatalog, HostFacts
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM


def _body(name: str, description: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {'name': name, 'description': description}
    payload.update(extra)
    return payload


def _host() -> HostFacts:
    return HostFacts(os='darwin', platform='arm64', shell='zsh')


def _models() -> dict[str, ScriptedLLM]:
    return {'default': ScriptedLLM([])}


def _spawn(factory):
    return factory(HooksCenter(), EventBus())


def test_os_platform_shell_are_interpolated_once_at_freeze():
    catalog = AgentCatalog()
    catalog.load_mapping(
        _body(
            'Worker',
            '改文件时用我',
            system_prompt='os={os} platform={platform} shell={shell}',
        )
    )
    factories = catalog.freeze({}, _models(), _host())
    first = _spawn(factories['Worker'])
    second = _spawn(factories['Worker'])
    assert first.options.system_prompt == 'os=darwin platform=arm64 shell=zsh'
    assert second.options.system_prompt == first.options.system_prompt
    assert '{os}' not in first.options.system_prompt
    assert '{platform}' not in first.options.system_prompt
    assert '{shell}' not in first.options.system_prompt


def test_cwd_in_the_prompt_is_rejected_even_if_the_host_knows_a_cwd():
    @dataclass(frozen=True)
    class _HostKnowingCwd(HostFacts):
        cwd: str = '/known/cwd'

    catalog = AgentCatalog()
    catalog.load_mapping(_body('Worker', '改文件时用我', system_prompt='dir={cwd}'))
    host = _HostKnowingCwd(os='darwin', platform='arm64', shell='zsh', cwd='/known/cwd')
    assert host.cwd == '/known/cwd'
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models(), host)
    error = excinfo.value
    assert '{cwd}' in error.received
    assert '历史块' in error.next_action
    assert 'System Prompt' in error.next_action


def test_unknown_placeholder_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_body('Worker', '改文件时用我', system_prompt='home={home}'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models(), _host())
    error = excinfo.value
    assert '{home}' in error.received
    assert '{os}' in error.expected
    assert '{platform}' in error.expected
    assert '{shell}' in error.expected


def test_double_braces_survive_as_a_literal_brace():
    catalog = AgentCatalog()
    catalog.load_mapping(_body('Worker', '改文件时用我', system_prompt='use {{os}} braces'))
    factories = catalog.freeze({}, _models(), _host())
    agent = _spawn(factories['Worker'])
    assert agent.options.system_prompt == 'use {os} braces'


def test_freeze_without_host_rejects_a_prompt_that_uses_placeholders():
    catalog = AgentCatalog()
    catalog.load_mapping(_body('Worker', '改文件时用我', system_prompt='os={os}'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models())
    error = excinfo.value
    assert 'HostFacts' in error.received or 'HostFacts' in error.next_action
    assert '{os}' in error.expected or '{os}' in error.received


def test_format_spec_on_a_placeholder_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_body('Worker', '改文件时用我', system_prompt='{os:posix}'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models(), _host())
    assert '{os:posix}' in excinfo.value.received


def test_unmatched_brace_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_body('Worker', '改文件时用我', system_prompt='hello {os'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models(), _host())
    assert '{os' in excinfo.value.received


def test_empty_prompt_can_freeze_without_host():
    catalog = AgentCatalog()
    catalog.load_mapping(_body('Worker', '改文件时用我'))
    factories = catalog.freeze({}, _models())
    agent = _spawn(factories['Worker'])
    assert agent.options.system_prompt == ''


def test_host_facts_blank_os_is_rejected():
    with pytest.raises(ConfigError) as excinfo:
        HostFacts(os=' ', platform='arm64', shell='zsh')
    assert 'os' in excinfo.value.received
