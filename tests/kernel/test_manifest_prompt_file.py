"""`system_prompt: {file: <相对路径>}`（M4 D11）：显式点名的文件一定读，写错直接报错；`prompt_file`
让宿主与清单共用「哪个文件会被读」这一条规则。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.manifest import ManifestSource, parse_mapping, prompt_file


def _source(origin: Path | None) -> ManifestSource:
    return ManifestSource(
        kind='yaml_file',
        location='config.yaml#agents[0]',
        origin=None if origin is None else str(origin),
    )


def _load(prompt: object, origin: Path | None) -> str:
    manifest = parse_mapping(
        {'name': 'Planner', 'description': 'plans', 'system_prompt': prompt},
        source=_source(origin),
    )
    return manifest.system_prompt


def _three_parts(error: BaseException) -> None:
    text = str(error)
    assert '期望:' in text
    assert '实际:' in text
    assert '下一步:' in text


def test_an_explicit_file_is_read(tmp_path: Path):
    (tmp_path / 'prompts').mkdir()
    (tmp_path / 'prompts' / 'planner.md').write_text('You plan.\n', encoding='utf-8')
    assert _load({'file': 'prompts/planner.md'}, tmp_path) == 'You plan.\n'


@pytest.mark.parametrize(
    ('prompt', 'anchor'),
    [
        ({'file': 'prompts/missing.md'}, '不存在或不是文件'),
        ({'file': 'prompts'}, '不存在或不是文件'),
        ({'file': '../outside.md'}, '在它之外'),
        ({'file': ' '}, '非空的相对路径'),
        ({'file': 1}, '只有一个键 file'),
        ({'file': 'prompts/planner.md', 'text': 'x'}, '只有一个键 file'),
        ({}, '只有一个键 file'),
    ],
)
def test_an_explicit_file_that_cannot_be_read_is_an_error_not_a_prompt(
    tmp_path: Path, prompt: object, anchor: str
):
    (tmp_path / 'prompts').mkdir()
    (tmp_path / 'prompts' / 'planner.md').write_text('You plan.', encoding='utf-8')
    (tmp_path.parent / 'outside.md').write_text('outside', encoding='utf-8')
    with pytest.raises(ConfigError) as excinfo:
        _load(prompt, tmp_path)
    assert anchor in f'{excinfo.value.expected}{excinfo.value.received}'
    _three_parts(excinfo.value)


def test_an_absolute_explicit_file_is_refused_even_inside_the_origin(tmp_path: Path):
    target = tmp_path / 'planner.md'
    target.write_text('You plan.', encoding='utf-8')
    with pytest.raises(ConfigError) as excinfo:
        _load({'file': str(target)}, tmp_path)
    assert '是绝对路径' in excinfo.value.received


def test_an_explicit_file_symlinked_out_of_the_origin_is_refused(tmp_path: Path):
    origin = tmp_path / 'config'
    origin.mkdir()
    secret = tmp_path / 'secret.md'
    secret.write_text('not for the model', encoding='utf-8')
    (origin / 'planner.md').symlink_to(secret)
    with pytest.raises(ConfigError) as excinfo:
        _load({'file': 'planner.md'}, origin)
    assert '在它之外' in excinfo.value.received


def test_an_explicit_file_needs_an_origin_to_resolve_against():
    with pytest.raises(ConfigError) as excinfo:
        _load({'file': 'prompts/planner.md'}, None)
    assert '没有所在目录' in excinfo.value.received
    assert '直接写进 system_prompt' in excinfo.value.next_action


def test_a_prompt_file_that_is_not_utf8_is_a_config_error(tmp_path: Path):
    (tmp_path / 'planner.md').write_bytes(b'\xff\xfe not utf-8')
    with pytest.raises(ConfigError) as excinfo:
        _load({'file': 'planner.md'}, tmp_path)
    assert 'utf-8' in excinfo.value.expected


def test_prompt_file_names_the_file_either_form_would_read(tmp_path: Path):
    target = tmp_path / 'planner.md'
    target.write_text('You plan.', encoding='utf-8')
    origin = str(tmp_path)
    assert prompt_file('planner.md', origin) == target.resolve()
    assert prompt_file({'file': 'planner.md'}, origin) == target.resolve()
    assert prompt_file('You plan.', origin) is None
    assert prompt_file('missing.md', origin) is None
    assert prompt_file('planner.md', None) is None


def test_without_an_origin_text_that_names_a_file_stays_text(tmp_path: Path, monkeypatch):
    (tmp_path / 'planner.md').write_text('from disk', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    assert _load('planner.md', None) == 'planner.md'


@pytest.mark.parametrize('prompt', [None, 3, ['planner.md']])
def test_a_system_prompt_of_another_type_is_refused(tmp_path: Path, prompt: object):
    with pytest.raises(ConfigError) as excinfo:
        _load(prompt, tmp_path)
    assert '{file: <相对路径>}' in excinfo.value.expected
