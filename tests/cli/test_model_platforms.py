"""清单 `model` 的 `<平台>:<型号>`（M4 D10）：按第一个 `:` 切，前缀命名空间封闭，带平台的名字只走
那一家。"""

from __future__ import annotations

import pytest

from syndica.__main__ import _configured_model  # pyright: ignore[reportPrivateUsage]
from syndica.backends.llm_router import ModelRouter
from syndica.kernel import ConfigError

_MODEL_ENV = (
    'GEMINI_API_KEY',
    'GEMINI_MODEL',
    'OPENAI_API_KEY',
    'OPENAI_BASE_URL',
    'OPENAI_MODEL',
    'OPENROUTER_API_KEY',
    'OPENROUTER_MODEL',
    'SYNDICA_LLM_ORDER',
)


def _only(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key in _MODEL_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    _only(
        monkeypatch,
        GEMINI_API_KEY='test-gemini-key',
        GEMINI_MODEL='gemini-env-choice',
        OPENROUTER_API_KEY='test-openrouter-key',
        OPENAI_BASE_URL='https://llm.example.test/v1',
        OPENAI_MODEL='env-model',
    )


def _members(llm: object) -> list[str]:
    assert isinstance(llm, ModelRouter)
    return [health.name for health in llm.health()]


@pytest.mark.parametrize(
    ('name', 'platform', 'model'),
    [
        ('gemini:gemini-3.8-flash', 'gemini', 'gemini-3.8-flash'),
        ('openrouter:cohere/north-mini-code:free', 'openrouter', 'cohere/north-mini-code:free'),
        ('openrouter:openrouter/free', 'openrouter', 'openrouter/free'),
        ('openai:qwen3:8b', 'openai', 'qwen3:8b'),
    ],
)
def test_a_platform_prefixed_name_goes_to_that_platform_only(
    monkeypatch: pytest.MonkeyPatch, name: str, platform: str, model: str
):
    _every_platform(monkeypatch)
    llm = _configured_model(name)
    assert _members(llm) == [platform]
    assert llm.config.model == model


@pytest.mark.parametrize(
    'name', ['cohere/north-mini-code:free', 'meta-llama/llama-4.1:free', 'openrouter/free']
)
def test_a_slash_or_dot_before_the_first_colon_makes_it_a_model_name(
    monkeypatch: pytest.MonkeyPatch, name: str
):
    _every_platform(monkeypatch)
    llm = _configured_model(name)
    assert _members(llm) == ['openrouter']
    assert llm.config.model == name


def test_an_unprefixed_name_without_openrouter_still_goes_to_the_generic_endpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    _only(monkeypatch, OPENAI_BASE_URL='https://llm.example.test/v1', OPENAI_MODEL='m')
    llm = _configured_model('vendor/model')
    assert _members(llm) == ['openai']
    assert llm.config.model == 'vendor/model'


@pytest.mark.parametrize(
    ('name', 'prefix', 'fix'),
    [
        ('Gemini:x', 'Gemini', '改成 gemini:x'),
        (' gemini:x', ' gemini', '改成 gemini:x'),
        ('gemini :x', 'gemini ', '改成 gemini:x'),
        ('OPENAI:m', 'OPENAI', '改成 openai:m'),
        ('gemni:x', 'gemni', "['gemini', 'openrouter', 'openai']"),
        ('qwen3:8b', 'qwen3', '写成 openai:qwen3:8b'),
        ('claude:sonnet', 'claude', '写成 openai:claude:sonnet'),
    ],
)
def test_a_name_like_prefix_must_be_one_of_the_three_platforms(
    monkeypatch: pytest.MonkeyPatch, name: str, prefix: str, fix: str
):
    _every_platform(monkeypatch)
    with pytest.raises(ConfigError) as excinfo:
        _configured_model(name)
    error = excinfo.value
    assert "['gemini', 'openrouter', 'openai']" in error.expected
    assert repr(name) in error.received
    assert repr(prefix) in error.received
    assert fix in error.next_action


@pytest.mark.parametrize('name', ['gemini:', 'openai:  '])
def test_a_platform_prefix_needs_a_model_after_it(monkeypatch: pytest.MonkeyPatch, name: str):
    _every_platform(monkeypatch)
    with pytest.raises(ConfigError) as excinfo:
        _configured_model(name)
    assert '型号非空' in excinfo.value.expected
    assert 'default' in excinfo.value.next_action


@pytest.mark.parametrize(
    ('name', 'variable'),
    [
        ('gemini:gemini-3.8-flash', 'GEMINI_API_KEY'),
        ('openrouter:vendor/model:free', 'OPENROUTER_API_KEY'),
        ('openai:m', 'OPENAI_BASE_URL'),
    ],
)
def test_an_unconfigured_platform_names_the_variable_to_set(
    monkeypatch: pytest.MonkeyPatch, name: str, variable: str
):
    _only(monkeypatch)
    with pytest.raises(ConfigError) as excinfo:
        _configured_model(name)
    assert variable in excinfo.value.expected


def test_an_explicit_gemini_model_beats_gemini_model_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    _every_platform(monkeypatch)
    assert _configured_model('gemini:gemini-3.8-pro').config.model == 'gemini-3.8-pro'
    assert _configured_model('default').config.model == 'gemini-env-choice'


@pytest.mark.parametrize('name', ['openrouter:vendor/paid-model', 'vendor/paid-model'])
def test_a_paid_openrouter_model_from_the_manifest_is_blamed_on_the_manifest(
    monkeypatch: pytest.MonkeyPatch, name: str
):
    _only(monkeypatch, OPENROUTER_API_KEY='test-openrouter-key')
    with pytest.raises(ConfigError) as excinfo:
        _configured_model(name)
    assert '清单的 model' in excinfo.value.expected + excinfo.value.received
    assert 'OPENROUTER_MODEL' not in excinfo.value.expected + excinfo.value.received


def test_a_paid_model_through_an_openrouter_generic_endpoint_is_blamed_on_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
):
    _only(monkeypatch, OPENAI_BASE_URL='https://openrouter.ai/api/v1', OPENAI_MODEL='x:free')
    with pytest.raises(ConfigError) as excinfo:
        _configured_model('openai:vendor/paid-model')
    assert '清单的 model' in excinfo.value.expected + excinfo.value.received
