"""发布验收的辅助脚本（scripts/acceptance_*.py）与假端点的剧本：验收的结论靠它们，它们自己也要有人看着。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import test_docs

REPO = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


readme = _load(REPO / 'scripts' / 'acceptance_readme.py', 'acceptance_readme')
export = _load(REPO / 'scripts' / 'acceptance_export.py', 'acceptance_export')
fake = _load(REPO / 'tests' / 'e2e' / 'fake_openai.py', 'fake_openai_for_tests')

_WHEEL = '/accept/syndica-9.9.9-py3-none-any.whl'


def _plan() -> Any:
    return readme.plan((REPO / 'README.md').read_text('utf-8'), _WHEEL)


def test_the_container_runs_the_same_readme_commands_the_docs_test_executes() -> None:
    executed = [
        line.strip()
        for block in test_docs.BLOCKS
        if block.page == REPO / 'README.md' and block.executed == 'quickstart'
        for line in block.text.splitlines()
        if line.strip()
    ]
    commands = [
        command.replace('"$(first_trace_id)"', '<trace-id>') for command in _plan().commands
    ]
    assert commands == executed
    assert any(command.startswith('syndica run ') for command in commands)


def test_the_only_change_to_the_install_command_is_the_package_source() -> None:
    plan = _plan()
    [(old, new)] = plan.substitutions
    assert old.strip('\'"') == 'syndica[openai]'
    assert new == f"'{_WHEEL}[openai]'"
    [install] = plan.install
    [original] = [
        line
        for block in readme.blocks((REPO / 'README.md').read_text('utf-8'))
        if block.section == 'Install'
        for line in block.lines
    ]
    assert install == original.replace(old, new)


def test_fixture_values_go_into_the_script_and_real_values_only_come_from_stdin() -> None:
    plan = _plan()
    values = {name: f'value-of-{name.lower()}' for name in plan.exported}
    fixture = readme.script(plan, values, [])
    for name, value in values.items():
        assert f'export {name}={value}' in fixture
    real = readme.script(plan, None, ['OPENAI_API_KEY', 'OPENAI_BASE_URL'])
    assert 'IFS= read -r OPENAI_API_KEY\nexport OPENAI_API_KEY' in real
    assert 'value-of' not in real and 'export OPENAI_MODEL=' not in real


def test_prepare_puts_the_secret_into_the_final_answer_of_the_release_task(tmp_path: Path) -> None:
    source = REPO / 'examples' / 'training-export' / 'offline' / 'script.json'
    target = tmp_path / 'script.json'

    export.prepare(source, target, 'sk-probe-quiz-0123')

    before = json.loads(source.read_text('utf-8'))['rules']
    after = json.loads(target.read_text('utf-8'))['rules']
    changed = [index for index, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 1
    rule = after[changed[0]]
    assert rule['when'] == 'version 1.2'
    assert rule['steps'][:-1] == before[changed[0]]['steps'][:-1]
    assert rule['steps'][-1]['content'].endswith('sk-probe-quiz-0123.')


def _exports(root: Path, *, dropped: list[str], extra: str = '') -> Path:
    for name, data in export.FORMATS.items():
        folder = root / name
        folder.mkdir(parents=True)
        samples = [{'id': 'kept', 'verdicts': ['pass']}]
        manifest = {'include_unverified': False, 'dropped_unverified': dropped, 'samples': samples}
        (folder / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
        row: dict[str, Any] = {'episode': {'id': 'kept'}} if name == 'rl' else {'id': 'kept'}
        (folder / data).write_text(json.dumps({**row, 'note': extra}) + '\n', 'utf-8')
    return root


def test_check_accepts_consistent_exports(tmp_path: Path) -> None:
    summary = export.check(_exports(tmp_path, dropped=['gone']), 'sk-quiz', 1, scripted=False)
    assert summary['sft'] == {'rows': 1, 'samples': ['pass'], 'dropped_unverified': 1}


def test_check_refuses_a_leaked_secret(tmp_path: Path) -> None:
    root = _exports(tmp_path, dropped=['gone'], extra='token sk-quiz')
    with pytest.raises(export.CheckFailed, match='密钥'):
        export.check(root, 'sk-quiz', 1, scripted=False)


def test_check_refuses_an_unverified_count_that_disagrees_with_the_batch(tmp_path: Path) -> None:
    with pytest.raises(export.CheckFailed, match='unverified'):
        export.check(_exports(tmp_path, dropped=[]), 'sk-quiz', 1, scripted=False)


# --- 假端点的剧本 -----------------------------------------------------------------------------


def _body(*messages: tuple[str, str], tools: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        'messages': [{'role': role, 'content': text} for role, text in messages],
        'tools': [{'type': 'function', 'function': {'name': name}} for name in tools],
    }


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    monkeypatch.setattr(fake, '_SCRIPT', rules)
    return rules


def _message(reply: dict[str, Any] | None) -> dict[str, Any]:
    assert reply is not None
    return reply['choices'][0]['message']


def test_steps_count_assistant_turns_after_the_latest_trigger(script: list[dict[str, Any]]) -> None:
    script.append({'when': 'go', 'steps': [{'content': 'one'}, {'content': 'two'}]})
    first = fake.scripted(_body(('system', 's'), ('user', 'go')), 1)
    second = fake.scripted(_body(('user', 'go'), ('assistant', 'one'), ('tool', 'x')), 2)
    again = fake.scripted(_body(('user', 'go'), ('assistant', 'one'), ('user', 'go')), 3)
    used_up = fake.scripted(_body(('user', 'go'), ('assistant', '1'), ('assistant', '2')), 4)
    assert [_message(r)['content'] for r in (first, second, again)] == ['one', 'two', 'one']
    assert used_up is None


def test_a_rule_needs_its_system_text_and_its_tool(script: list[dict[str, Any]]) -> None:
    script.append({'system': 'verifier', 'tool': 'Write', 'reply': {'content': 'judged'}})
    assert fake.scripted(_body(('system', 'worker'), ('user', 'x'), tools=('Write',)), 1) is None
    assert fake.scripted(_body(('system', 'verifier'), ('user', 'x')), 2) is None
    hit = fake.scripted(_body(('system', 'verifier'), ('user', 'x'), tools=('Write',)), 3)
    assert _message(hit)['content'] == 'judged'


def test_tool_calls_get_distinct_ids_and_json_arguments(script: list[dict[str, Any]]) -> None:
    calls = [{'name': 'Read', 'arguments': {'path': 'a'}}, {'name': 'Read', 'arguments': {}}]
    script.append({'reply': {'tool_calls': calls}})
    message = _message(fake.scripted(_body(('user', 'x')), 7))
    assert [call['id'] for call in message['tool_calls']] == ['call-7-0', 'call-7-1']
    assert json.loads(message['tool_calls'][0]['function']['arguments']) == {'path': 'a'}


def test_projected_rubrics_anchor_to_the_source_that_holds_the_quote(
    script: list[dict[str, Any]],
) -> None:
    script.append({'reply': {'rubrics': [{'id': 'R1', 'statement': 's', 'quote': 'four'}]}})
    task = '\n'.join(
        [
            '任务目标：x',
            '',
            '可引用的来源（locator —— 原文）：',
            '- msg:goal：section one',
            '- msg:7f3：section four',
        ]
    )
    reply = json.loads(_message(fake.scripted(_body(('user', task)), 1))['content'])
    [rubric] = reply['rubrics']
    assert rubric['anchors'] == [{'source': 'user_message', 'locator': 'msg:7f3', 'quote': 'four'}]
    assert rubric['kind'] == 'checkable'
