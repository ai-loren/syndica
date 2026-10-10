"""README 与 docs/user 下每一页：代码块、链接与参考页都对着真的 CLI 解析器、配置加载器与声明核对。

写文档的约定（用例按它判）：

- 不执行也不解析的代码块，紧挨开围栏的上一行写 `<!-- not-tested: 原因 -->`，原因不能空。
- bash / sh / shell / console 块里每条 `syndica ...` 命令都过真的参数解析器（`main` 配上只记录调用的
  宿主）；`--flag` 再对内置 Module 的声明校验。`pip` / `pipx` / `uv sync` 点名的 extra 必须在
  pyproject 里；`export` 的变量名必须是 syndica 源码里出现过的名字。`<名字>` 形式的占位换成一个词
  再解析。
- yaml 块：根键都是 `.syndica.yaml` 的键时用真的配置加载器加载（缺 `version` 时补上），再核对
  `modules:` 与 `flags:`；根只有 `credentials` 时用用户级 `interop.yaml` 的加载器。别的形状不收。
- python 块在临时目录的子进程里离线跑完，退出码 0。json / jsonl / toml 块要能解析。
- 其余语言（text 等）必须标 not-tested。
- 标了 `<!-- executed: quickstart -->` 的块按页面顺序在假的 OpenAI 端点前真的执行一遍。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

from syndica.cli import config as config_module
from syndica.cli import interop_config
from syndica.cli.config import load_config
from syndica.cli.flags import resolved_flags
from syndica.cli.host import OPTIONAL_BUILTINS, reference_modules
from syndica.cli.interop_config import load_interop_credentials
from syndica.cli.main import (
    CliIO,
    CliOutcome,
    InspectRequest,
    MemoryRequest,
    RunRequest,
    SessionsRequest,
    TraceRequest,
    main,
)
from syndica.kernel import FlagDeclaration, flag_reference
from syndica.kernel import manifest as manifest_module

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / 'docs' / 'user'
PAGES = (REPO_ROOT / 'README.md', *sorted(DOCS_ROOT.rglob('*.md')))
REPO_URL = 'https://github.com/ai-loren/syndica'
FAKE_OPENAI = REPO_ROOT / 'tests' / 'e2e' / 'fake_openai.py'
SHELL = frozenset({'bash', 'sh', 'shell', 'console'})
TESTED = SHELL | {'yaml', 'yml', 'python', 'py', 'json', 'jsonl', 'toml'}
_FENCE_OPEN = re.compile(r'^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^`]*)$')
_MARKER = re.compile(r'^\s*<!--\s*(?P<kind>not-tested|executed)\s*:\s*(?P<text>.*?)\s*-->\s*$')
_PLACEHOLDER = re.compile(r'<([A-Za-z][\w.-]*)>')
_SEPARATORS = frozenset({'&&', '||', ';', '|', '&'})
_ENV_PREFIXES = ('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_', 'OTEL_')


@dataclass(frozen=True)
class Block:
    page: Path
    line: int
    lang: str
    text: str
    not_tested: str | None
    executed: str | None

    @property
    def where(self) -> str:
        return f'{self.page.relative_to(REPO_ROOT)}:{self.line}'


def _blocks(page: Path) -> list[Block]:
    lines = page.read_text(encoding='utf-8').split('\n')
    found: list[Block] = []
    index = 0
    while index < len(lines):
        opened = _FENCE_OPEN.match(lines[index])
        if opened is None:
            index += 1
            continue
        indent, fence = opened.group('indent'), opened.group('fence')
        lang = (opened.group('info').strip().split() or [''])[0].lower()
        body: list[str] = []
        end = index + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped.startswith(fence[0] * len(fence)) and set(stripped) == {fence[0]}:
                break
            body.append(lines[end][len(indent) :] if lines[end].startswith(indent) else lines[end])
            end += 1
        markers: dict[str, str] = {}
        back = index - 1
        while back >= 0 and (marker := _MARKER.match(lines[back])) is not None:
            markers.setdefault(marker.group('kind'), marker.group('text'))
            back -= 1
        found.append(
            Block(
                page=page,
                line=index + 1,
                lang=lang,
                text='\n'.join(body) + '\n',
                not_tested=markers.get('not-tested'),
                executed=markers.get('executed'),
            )
        )
        index = end + 1
    return found


BLOCKS = [block for page in PAGES for block in _blocks(page)]


def _checked(*langs: str) -> list[Block]:
    return [block for block in BLOCKS if block.lang in langs and block.not_tested is None]


def _ids(blocks: Sequence[Block]) -> list[str]:
    return [block.where for block in blocks]


# --- 每个块要么被测、要么写明为什么不测 --------------------------------------------------------


def test_the_pages_have_code_blocks_to_check() -> None:
    assert len(PAGES) > 20
    assert {block.lang for block in BLOCKS} >= {'bash', 'yaml', 'python', 'text'}


@pytest.mark.parametrize('block', BLOCKS, ids=_ids(BLOCKS))
def test_every_block_is_tested_or_says_why_not(block: Block) -> None:
    if block.not_tested is not None:
        assert block.not_tested, (
            f'期望: {block.where} 的 not-tested 标记写明不测的原因。实际: 原因是空的。'
            ' 下一步: 写成 <!-- not-tested: 原因 -->。'
        )
        assert block.executed is None, (
            f'期望: {block.where} 不同时标 not-tested 与 executed。实际: 两个都标了。'
            ' 下一步: 删掉其中一个。'
        )
        return
    assert block.lang in TESTED, (
        f'期望: {block.where} 的代码块是会被测的语言 {sorted(TESTED)}，或标了 not-tested。'
        f' 实际: 语言是 {block.lang!r}，没有标记。'
        ' 下一步: 紧挨开围栏的上一行加 <!-- not-tested: 原因 -->，或换成会被测的语言。'
    )


# --- shell 块：syndica 命令过真的解析器 ------------------------------------------------------


class _RecordingHost:
    """CLI 宿主的替身：解析通过之后每个入口都交回空结果，什么都不执行。"""

    def run_request(self, request: RunRequest) -> str:
        return ''

    def inspect(self, request: InspectRequest) -> str:
        return ''

    def doctor(self, config: str, *, verify: bool = False) -> str:
        return ''

    def open_chat(
        self,
        config: str,
        max_turns: int | None = None,
        *,
        trace: str | None = None,
        argv: Sequence[str] = (),
        trusted_output: Sequence[str] = (),
        resume: str | None = None,
        goal: str | None = None,
        on_exhausted: str | None = None,
    ) -> str:
        return 'Orchestrator'

    def active_agents(self) -> str:
        return ''

    def trust_config(self, config: str, sha256: str | None) -> str:
        return ''

    def trace(self, request: TraceRequest) -> str:
        return ''

    def sessions(self, request: SessionsRequest) -> str:
        return ''

    def memory(self, request: MemoryRequest) -> str:
        return ''


class _RecordingServer:
    def up(
        self,
        config: str,
        *,
        port: int,
        foreground: bool,
        open_browser: bool,
        api_token: bool = False,
    ) -> str:
        return ''

    def down(self) -> str:
        return ''

    def status(self) -> str:
        return ''

    def acp(self, config: str) -> str:
        return ''

    def mcp(self, config: str) -> str:
        return ''


class _RecordingTraining:
    def batch(self, dataset: str, config: str) -> str:
        return ''

    def export(
        self, fmt: str, *, config: str, output: str, include_unverified: bool
    ) -> str | CliOutcome:
        return ''


def _parser() -> argparse.ArgumentParser:
    from syndica.cli.main import _parser as build  # pyright: ignore[reportPrivateUsage]

    return build()


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            return dict(cast(dict[str, argparse.ArgumentParser], action.choices))
    return {}


COMMANDS = frozenset(_subparsers(_parser()))


def _all_flag_declarations() -> list[FlagDeclaration]:
    return [declaration for module in reference_modules() for declaration in module.flags]


def _extras() -> set[str]:
    project = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    return set(project['project']['optional-dependencies'])


def _source_text() -> str:
    return '\n'.join(
        path.read_text(encoding='utf-8') for path in (REPO_ROOT / 'src' / 'syndica').rglob('*.py')
    )


SOURCE_TEXT = _source_text()


@dataclass(frozen=True)
class Token:
    raw: str

    @property
    def value(self) -> str:
        raw = self.raw
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in '\'"':
            return raw[1:-1]
        return raw


def _commands(block: Block) -> Iterator[list[Token]]:
    """一个 shell 块里的每条命令。续行先拼起来；console 块只认 `$ ` 开头的行。"""
    logical: list[str] = []
    pending = ''
    for line in block.text.split('\n'):
        if block.lang == 'console':
            if not line.startswith('$ ') and not pending:
                continue
            line = line.removeprefix('$ ')
        if line.rstrip().endswith('\\'):
            pending += line.rstrip()[:-1] + ' '
            continue
        logical.append(pending + line)
        pending = ''
    for line in logical:
        lexer = shlex.shlex(_PLACEHOLDER.sub(r'x-\1', line), posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        command: list[Token] = []
        for raw in lexer:
            if raw in _SEPARATORS:
                if command:
                    yield command
                command = []
            else:
                command.append(Token(raw))
        if command:
            yield command


def _syndica_argv(tokens: list[Token]) -> list[Token] | None:
    """这条命令里交给 syndica 的参数；不是 syndica 命令时 None。"""
    values = [token.value for token in tokens]
    if values[:2] == ['uv', 'run']:
        return _syndica_argv(tokens[2:]) if len(tokens) > 2 else None
    if values[:1] == ['syndica']:
        return tokens[1:]
    if len(values) >= 3 and values[0].startswith('python') and values[1:3] == ['-m', 'syndica']:
        return tokens[3:]
    return None


def _first_positional(argv: list[Token]) -> Token | None:
    index = 0
    while index < len(argv):
        if argv[index].value == '--config':
            index += 2
            continue
        if argv[index].value.startswith('--config='):
            index += 1
            continue
        return argv[index]
    return None


def _check_syndica(block: Block, argv: list[Token]) -> None:
    shown = 'syndica ' + ' '.join(token.raw for token in argv)
    first = _first_positional(argv)
    if first is not None and first.value not in COMMANDS and first.raw == first.value:
        pytest.fail(
            f'期望: {block.where} 的 `{shown}` 以命令名开头，或者是用引号括起来的 prompt'
            f'（隐式 run）。 实际: {first.value!r} 既不是命令（{sorted(COMMANDS)}）也没有引号，'
            '会被当成 prompt。'
            ' 下一步: 改成真实的命令名；本意是 prompt 就加引号。'
        )
    out, err = StringIO(), StringIO()
    code = main(
        [token.value for token in argv],
        host=_RecordingHost(),
        io=CliIO(stdin=StringIO(''), stdout=out, stderr=err),
        server=_RecordingServer(),
        training=_RecordingTraining(),
    )
    assert (code, err.getvalue()) == (0, ''), (
        f'期望: {block.where} 的 `{shown}` 能被 syndica 的参数解析器接受。'
        f' 实际: 退出码 {code}，stderr:\n{err.getvalue()}'
        ' 下一步: 对照 `syndica <命令> --help` 改文档里的命令或选项。'
    )
    values = [token.value for token in argv]
    flags = {
        value.partition('=')[0]: value.partition('=')[2]
        for option, value in zip(values, values[1:], strict=False)
        if option == '--flag'
    }
    if flags:
        resolved_flags({}, flags, _all_flag_declarations())


def _check_extras(block: Block, tokens: list[Token]) -> None:
    named: set[str] = set()
    for token in tokens:
        match = re.fullmatch(r'(?:syndica|\.)\[([\w,-]+)\]', token.value)
        if match is not None:
            named |= set(match.group(1).split(','))
    values = [token.value for token in tokens]
    named |= {
        value for option, value in zip(values, values[1:], strict=False) if option == '--extra'
    }
    unknown = sorted(named - _extras())
    assert not unknown, (
        f'期望: {block.where} 点名的 extra 都在 pyproject.toml 的 optional-dependencies 里。'
        f' 实际: 没有 {unknown}，现有 {sorted(_extras())}。'
        ' 下一步: 改成已有的 extra 名。'
    )


def _check_export(block: Block, tokens: list[Token]) -> None:
    for token in tokens[1:]:
        name = token.value.partition('=')[0]
        assert f"'{name}'" in SOURCE_TEXT or f'"{name}"' in SOURCE_TEXT, (
            f'期望: {block.where} export 的 {name} 是 syndica 会读的环境变量。'
            f' 实际: src/syndica 里没有出现过字符串 {name!r}。'
            ' 下一步: 改成真实的变量名，或者删掉这一行。'
        )


SHELL_BLOCKS = _checked(*SHELL)


@pytest.mark.parametrize('block', SHELL_BLOCKS, ids=_ids(SHELL_BLOCKS))
def test_shell_commands_parse(block: Block) -> None:
    for tokens in _commands(block):
        values = [token.value for token in tokens]
        argv = _syndica_argv(tokens)
        if argv is not None:
            _check_syndica(block, argv)
        elif values[0] in {'pip', 'pipx'} or 'pip' in values[:3]:
            _check_extras(block, tokens)
        elif values[:2] == ['uv', 'sync']:
            _check_extras(block, tokens)
        elif values[0] == 'export':
            _check_export(block, tokens)


def test_the_cli_check_reads_every_documented_syndica_command() -> None:
    commands = [
        argv
        for block in SHELL_BLOCKS
        for tokens in _commands(block)
        if (argv := _syndica_argv(tokens)) is not None
    ]
    assert len(commands) > 40
    documented = {argv[0].value for argv in commands if argv}
    assert documented >= {'run', 'trace', 'up', 'down', 'doctor', 'config', 'sessions', 'export'}


# --- yaml 块 -----------------------------------------------------------------------------


def _prompt_files(raw: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for agent in raw.get('agents') or ():
        if isinstance(agent, dict):
            prompt = cast(dict[str, Any], agent).get('system_prompt')
            if isinstance(prompt, dict) and isinstance(prompt.get('file'), str):
                files.append(cast(str, prompt['file']))
    return files


def _check_project_config(block: Block, raw: dict[str, Any], tmp_path: Path) -> None:
    text = block.text if 'version' in raw else 'version: 1\n' + block.text
    for name in _prompt_files(raw):
        prompt = tmp_path / name
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text('A prompt.\n', encoding='utf-8')
    path = tmp_path / '.syndica.yaml'
    path.write_text(text, encoding='utf-8')
    loaded = load_config(path)
    unknown = sorted(set(loaded.modules) - set(OPTIONAL_BUILTINS))
    assert not unknown, (
        f'期望: {block.where} 的 modules 都是内置的可选 Module {list(OPTIONAL_BUILTINS)}。'
        f' 实际: 还有 {unknown}。'
        ' 下一步: 第三方 Module 的例子标 not-tested，或改成内置 Module。'
    )
    installed = {'safety', 'human-interaction', *loaded.modules}
    declarations = [
        declaration
        for module in reference_modules()
        if module.name in installed
        for declaration in module.flags
    ]
    resolved_flags(loaded.flags, {}, declarations)


YAML_BLOCKS = _checked('yaml', 'yml')


@pytest.mark.parametrize('block', YAML_BLOCKS, ids=_ids(YAML_BLOCKS))
def test_yaml_blocks_load_with_the_real_loaders(block: Block, tmp_path: Path) -> None:
    raw = yaml.safe_load(block.text)
    assert isinstance(raw, dict), f'期望: {block.where} 的 yaml 是一个映射。实际: {type(raw)}。'
    keys = set(cast(dict[str, Any], raw))
    root_keys = config_module._ROOT_KEYS  # pyright: ignore[reportPrivateUsage]
    if keys <= root_keys:
        _check_project_config(block, cast(dict[str, Any], raw), tmp_path)
        return
    assert keys == {'credentials'}, (
        f'期望: {block.where} 是 .syndica.yaml（根键 {sorted(root_keys)}）或用户级 interop.yaml'
        f'（根键 credentials）的例子。 实际: 根键 {sorted(keys)}。'
        ' 下一步: 改成这两种之一，或标 <!-- not-tested: 原因 -->。'
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    path = tmp_path / 'config' / 'interop.yaml'
    path.parent.mkdir()
    path.write_text(block.text, encoding='utf-8')
    path.chmod(0o600)
    assert load_interop_credentials(path, workspace=workspace)


# --- python / json / toml 块 ---------------------------------------------------------------


def _offline_environ(**extra: str) -> dict[str, str]:
    environ = {key: value for key, value in os.environ.items() if not key.startswith(_ENV_PREFIXES)}
    environ.update(extra)
    return environ


PYTHON_BLOCKS = _checked('python', 'py')


@pytest.mark.parametrize('block', PYTHON_BLOCKS, ids=_ids(PYTHON_BLOCKS))
def test_python_blocks_run_offline(block: Block, tmp_path: Path) -> None:
    script = tmp_path / 'block.py'
    script.write_text(block.text, encoding='utf-8')
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=_offline_environ(XDG_CONFIG_HOME=str(tmp_path / 'config')),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f'期望: {block.where} 的 python 块离线跑完、退出码 0。'
        f' 实际: 退出码 {completed.returncode}，stderr:\n{completed.stderr}'
        ' 下一步: 修好例子；它确实跑不了（要网络、要别的服务）就标 not-tested 并写明原因。'
    )


DATA_BLOCKS = _checked('json', 'jsonl', 'toml')


@pytest.mark.parametrize('block', DATA_BLOCKS, ids=_ids(DATA_BLOCKS))
def test_data_blocks_parse(block: Block) -> None:
    if block.lang == 'toml':
        tomllib.loads(block.text)
    elif block.lang == 'json':
        json.loads(block.text)
    else:
        rows = [json.loads(line) for line in block.text.splitlines() if line.strip()]
        assert rows


# --- 链接 ----------------------------------------------------------------------------------


def _load_check_links() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        'check_links', REPO_ROOT / 'scripts' / 'check_links.py'
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK_LINKS = _load_check_links()


def _slug(heading: str) -> str:
    """Python-Markdown toc 的默认锚点：去掉标记与标点、小写、空白换成连字符。"""
    text = re.sub(r'`([^`]*)`', r'\1', heading)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '-', text)


def _anchors(page: Path) -> set[str]:
    visible = cast(str, CHECK_LINKS._without_fences(page.read_text(encoding='utf-8')))  # pyright: ignore[reportPrivateUsage]
    return {
        _slug(match.group(1)) for match in re.finditer(r'^#{1,6}\s+(.+?)\s*#*\s*$', visible, re.M)
    }


def _links(page: Path) -> list[tuple[int, str]]:
    return cast(list[tuple[int, str]], CHECK_LINKS.targets(page.read_text(encoding='utf-8')))


def _strip(raw: str) -> str:
    return raw[1:-1] if raw.startswith('<') and raw.endswith('>') else raw


@pytest.mark.parametrize('page', PAGES, ids=[str(page.relative_to(REPO_ROOT)) for page in PAGES])
def test_relative_links_resolve_and_stay_publishable(page: Path) -> None:
    for line, raw in _links(page):
        target = _strip(raw)
        where = f'{page.relative_to(REPO_ROOT)}:{line}'
        if re.match(r'[A-Za-z][\w+.-]*:', target):
            _check_repository_url(where, target)
            continue
        path_part, _, anchor = target.partition('#')
        resolved = (page.parent / path_part).resolve() if path_part else page
        assert resolved.exists(), (
            f'期望: {where} 的链接 {target!r} 指向存在的文件。实际: {resolved} 不存在。'
            ' 下一步: 改成存在的相对路径。'
        )
        if page.is_relative_to(DOCS_ROOT):
            assert resolved.is_relative_to(DOCS_ROOT), (
                f'期望: 文档站页面 {where} 只用相对链接指向 docs/user 之内。'
                f' 实际: {target!r} 指到 {resolved}，站点里没有它。'
                f' 下一步: 改成 {REPO_URL}/blob/main/<路径> 这样的绝对链接。'
            )
        if anchor and resolved.suffix == '.md':
            assert anchor in _anchors(resolved), (
                f'期望: {where} 的锚点 #{anchor} 是 {resolved.name} 里某个标题的锚点。'
                f' 实际: 那里的锚点是 {sorted(_anchors(resolved))}。'
                ' 下一步: 改成存在的标题锚点。'
            )


def _check_repository_url(where: str, url: str) -> None:
    match = re.fullmatch(rf'{re.escape(REPO_URL)}/(?:blob|tree)/main/([^#?]+)(?:[#?].*)?', url)
    if match is None:
        return
    relative = match.group(1).rstrip('/')
    assert (REPO_ROOT / relative).exists(), (
        f'期望: {where} 链到本仓 main 上的 {relative}，它在仓库里。实际: 没有这个路径。'
        ' 下一步: 改成存在的路径。'
    )


# --- 导航 ----------------------------------------------------------------------------------


def _nav_pages(nav: object) -> list[str]:
    if isinstance(nav, str):
        return [nav]
    if isinstance(nav, list):
        return [page for item in cast(list[object], nav) for page in _nav_pages(item)]
    if isinstance(nav, dict):
        return [page for item in cast(dict[str, object], nav).values() for page in _nav_pages(item)]
    return []


def test_every_page_is_in_the_navigation_and_every_entry_exists() -> None:
    site = yaml.safe_load((REPO_ROOT / 'mkdocs.yml').read_text(encoding='utf-8'))
    assert site['docs_dir'] == 'docs/user'
    listed = _nav_pages(site['nav'])
    assert len(listed) == len(set(listed))
    present = {page.relative_to(DOCS_ROOT).as_posix() for page in DOCS_ROOT.rglob('*.md')}
    assert present - set(listed) == set(), '期望: docs/user 下每一页都在 mkdocs.yml 的 nav 里。'
    missing = set(listed) - present
    assert missing == set(), f'期望: nav 的每一项都存在。实际: 没有 {sorted(missing)}。'


# --- 参考页对着代码 --------------------------------------------------------------------------


def _sections(text: str) -> dict[str, str]:
    """`## syndica ...` 这类标题 → 到下一个任意级标题为止的正文。"""
    parts = re.split(r'^#{2,6}\s+(syndica[^\n]*)$', text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def _leaf_commands(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    children = _subparsers(parser)
    if not children and path:
        yield path, parser
    for name, child in children.items():
        yield from _leaf_commands(child, (*path, name))


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions  # pyright: ignore[reportPrivateUsage]
        for option in action.option_strings
        if option.startswith('--') and option != '--help'
    }


def test_the_cli_reference_documents_every_command_and_option_and_nothing_else() -> None:
    sections = _sections((DOCS_ROOT / 'reference' / 'cli.md').read_text(encoding='utf-8'))
    leaves = list(_leaf_commands(_parser()))
    assert len(leaves) > 30
    for path, parser in leaves:
        title = 'syndica ' + ' '.join(path)
        assert title in sections, f'期望: CLI 参考页有一节 `{title}`。实际: 没有。'
        body = sections[title]
        options = _options(parser)
        mentioned = set(re.findall(r'(?<![\w-])--[a-z][a-z0-9-]*', body))
        assert options <= mentioned, (
            f'期望: `{title}` 一节写到它的每个选项。实际: 缺 {sorted(options - mentioned)}。'
        )
        assert mentioned <= options | {'--config'}, (
            f'期望: `{title}` 一节只写它真有的选项。实际: 多出 {sorted(mentioned - options)}。'
        )


def _flag_rows(text: str) -> dict[str, tuple[str, str, str]]:
    rows: dict[str, tuple[str, str, str]] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) >= 4 and re.fullmatch(r'`[a-z][a-z0-9_]*`', cells[0]):
            rows[cells[0].strip('`')] = (cells[1].strip('`'), cells[2], cells[3].strip('`'))
    return rows


def test_the_flag_reference_matches_the_declarations() -> None:
    page = _flag_rows((DOCS_ROOT / 'reference' / 'flags.md').read_text(encoding='utf-8'))
    expected = {
        row.key: (row.module, row.kind, json.dumps(row.default))
        for row in flag_reference(reference_modules())
    }
    assert page == expected


def test_every_flag_of_the_generated_table_is_on_the_english_page() -> None:
    generated = _flag_rows((REPO_ROOT / 'docs' / 'reference' / 'flags.md').read_text('utf-8'))
    page = _flag_rows((DOCS_ROOT / 'reference' / 'flags.md').read_text(encoding='utf-8'))
    assert len(generated) > 20
    assert sorted(set(generated) - set(page)) == []


def test_the_configuration_reference_names_every_key_the_loader_accepts() -> None:
    text = (DOCS_ROOT / 'reference' / 'configuration.md').read_text(encoding='utf-8')
    documented = set(re.findall(r'`([a-z][a-z0-9_]*)`', text))
    private = cast(dict[str, object], vars(config_module)) | cast(
        dict[str, object], vars(interop_config)
    )
    key_sets = [value for name, value in private.items() if name.endswith('_KEYS')]
    key_sets += [private['_CREDENTIAL_ROOT'], private['_CREDENTIAL_FIELDS']]
    fields = cast(tuple[str, ...], vars(manifest_module)['_PUBLIC_FIELDS'])
    expected = {key for keys in key_sets for key in cast(frozenset[str], keys)} | {*fields, 'impl'}
    assert len(expected) > 40
    assert sorted(expected - documented) == []


def test_the_http_reference_lists_every_route_of_the_snapshot() -> None:
    text = (DOCS_ROOT / 'reference' / 'http-api.md').read_text(encoding='utf-8')
    snapshot = json.loads((REPO_ROOT / 'docs' / 'reference' / 'openapi.json').read_text('utf-8'))
    paths = sorted(snapshot['paths'])
    assert len(paths) > 25
    assert [path for path in paths if f'`{path}`' not in text] == []


# --- 五分钟上手：真的跑一遍 ------------------------------------------------------------------

_FAKE_KEY = 'docs-e2e-dummy-key-0123456789'


def _fake_value(name: str, port: int) -> str:
    values = {
        'OPENAI_BASE_URL': f'http://127.0.0.1:{port}/v1',
        'OPENAI_MODEL': 'fake',
        'OPENAI_API_KEY': _FAKE_KEY,
        'SYNDICA_LLM_ORDER': 'openai',
    }
    if name not in values:
        pytest.fail(
            f'期望: 上手页 export 的变量都有假端点下的取值 {sorted(values)}。实际: 还有 {name}。'
            ' 下一步: 在 tests/test_docs.py 的 _fake_value 里给它一个指向假端点的值。'
        )
    return values[name]


@contextlib.contextmanager
def _fake_openai() -> Iterator[int]:
    process = subprocess.Popen(
        [sys.executable, str(FAKE_OPENAI)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield int(process.stdout.readline().strip())
    finally:
        process.terminate()
        process.wait(timeout=10)


def _newest_trace(cwd: Path) -> str:
    traces = sorted(path.name for path in (cwd / '.syndica' / 'traces').iterdir() if path.is_dir())
    assert traces, f'期望: {cwd} 里已经有 trace。实际: .syndica/traces 是空的。'
    return traces[-1]


EXECUTED_PAGES = sorted({block.page for block in BLOCKS if block.executed == 'quickstart'})


def test_the_readme_and_the_quickstart_page_both_have_an_executed_walkthrough() -> None:
    assert EXECUTED_PAGES == [REPO_ROOT / 'README.md', DOCS_ROOT / 'quickstart.md']


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    'page', EXECUTED_PAGES, ids=[str(page.relative_to(REPO_ROOT)) for page in EXECUTED_PAGES]
)
def test_the_quickstart_runs_end_to_end(page: Path, tmp_path: Path) -> None:
    blocks = [block for block in BLOCKS if block.page == page]
    exported = [
        token.value.partition('=')[0]
        for block in blocks
        if block.lang in SHELL and block.executed is None
        for tokens in _commands(block)
        if tokens[0].value == 'export'
        for token in tokens[1:]
    ]
    assert 'OPENAI_BASE_URL' in exported
    server_extra = importlib.util.find_spec('starlette') is not None
    root = tmp_path / 'work'
    root.mkdir()
    with _fake_openai() as port:
        environ = _offline_environ(
            XDG_CONFIG_HOME=str(tmp_path / 'config'),
            BROWSER='true',
            **{name: _fake_value(name, port) for name in exported},
        )
        cwd = root
        ran: list[str] = []
        planned: list[str] = []
        try:
            for block in blocks:
                if block.executed != 'quickstart':
                    continue
                for tokens in _commands(block):
                    values = [token.value for token in tokens]
                    if values[0] == 'mkdir':
                        (cwd / values[1]).mkdir()
                        continue
                    if values[0] == 'cd':
                        cwd = cwd / values[1]
                        continue
                    argv = _syndica_argv(tokens)
                    assert argv is not None, (
                        f'期望: {block.where} 这个 executed 块只有 syndica、mkdir、cd 命令。'
                        f' 实际: {" ".join(values)}。 下一步: 把它挪到不执行的块里。'
                    )
                    args = [
                        _newest_trace(cwd) if value == 'x-trace-id' else value
                        for value in (token.value for token in argv)
                    ]
                    planned.append(args[0])
                    if args[0] in {'up', 'down', 'status'} and not server_extra:
                        continue
                    completed = subprocess.run(
                        [sys.executable, '-P', '-m', 'syndica', *args],
                        cwd=cwd,
                        env=environ,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    ran.append(args[0])
                    said = completed.stdout + completed.stderr
                    assert completed.returncode == 0, (
                        f'期望: {block.where} 的 `syndica {" ".join(args)}` 退出码 0。'
                        f' 实际: 退出码 {completed.returncode}，输出:\n{said}'
                    )
                    assert _FAKE_KEY not in said
        finally:
            if 'up' in ran:
                subprocess.run(
                    [sys.executable, '-P', '-m', 'syndica', 'down'],
                    cwd=cwd,
                    env=environ,
                    capture_output=True,
                    timeout=60,
                )
    skipped = set() if server_extra else {'up', 'down', 'status'}
    assert ran == [command for command in planned if command not in skipped]
    assert 'run' in ran and 'trace' in ran
    assert (cwd / '.syndica' / 'sessions').is_dir()
