"""发布验收 (a)：把 README 的安装与上手命令原样写成一个在干净容器里跑的 shell 脚本。

取 README 里这几样，别的都不取：

- 「## Install」一节里的 `pip install` 命令。唯一的替换：`syndica[...]` 这个包来源换成拷进
  容器的 wheel（`<wheel>[...]`），extra 照抄。
- 上手一节 `export` 的变量名。值是用户自己的端点：README 里写的是占位（`your-key`），这里给的
  是夹具端点的值（或真模型模式下从 stdin 读进来的值，不落盘）。
- 标了 `<!-- executed: quickstart -->` 的块里的每条命令，逐字照抄（与 tests/test_docs.py
  执行的是同一批）。其中的占位 `<trace-id>` 按 README 说的办法填：`syndica trace list` 第一行的 id。

块的语法与 tests/test_docs.py 一致：围栏、紧挨开围栏上一行的标记、续行。只用标准库。

用法：`python acceptance_readme.py README.md <容器里的 wheel> <输出脚本> [--from-stdin 变量,…]`；
把取到的命令与做过的替换以 JSON 打到 stdout（审计记录照它写）。
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

_FENCE_OPEN = re.compile(r'^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^`]*)$')
_MARKER = re.compile(r'^\s*<!--\s*(?P<kind>not-tested|executed)\s*:\s*(?P<text>.*?)\s*-->\s*$')
_PACKAGE = re.compile(r"""^(?P<quote>['"]?)syndica(?P<extras>\[[\w,-]+\])?(?P=quote)$""")
_SHELL = frozenset({'bash', 'sh', 'shell', 'console'})
_TRACE_ID = '<trace-id>'
_FIRST_TRACE = '"$(first_trace_id)"'


class ReadmeError(Exception):
    pass


@dataclass(frozen=True)
class Block:
    line: int
    lang: str
    lines: tuple[str, ...]
    section: str
    executed: str | None


def blocks(text: str) -> list[Block]:
    lines = text.split('\n')
    found: list[Block] = []
    section = ''
    index = 0
    while index < len(lines):
        if lines[index].startswith('## '):
            section = lines[index][3:].strip()
        opened = _FENCE_OPEN.match(lines[index])
        if opened is None:
            index += 1
            continue
        fence = opened.group('fence')
        lang = (opened.group('info').strip().split() or [''])[0].lower()
        body: list[str] = []
        end = index + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped.startswith(fence[0] * len(fence)) and set(stripped) == {fence[0]}:
                break
            body.append(lines[end])
            end += 1
        executed = None
        back = index - 1
        while back >= 0 and (marker := _MARKER.match(lines[back])) is not None:
            if marker.group('kind') == 'executed':
                executed = marker.group('text')
            back -= 1
        found.append(Block(index + 1, lang, _logical(body), section, executed))
        index = end + 1
    return found


def _logical(body: list[str]) -> tuple[str, ...]:
    """续行拼起来、去掉空行与注释行。"""
    out: list[str] = []
    pending = ''
    for line in body:
        if line.rstrip().endswith('\\'):
            pending += line.rstrip()[:-1] + ' '
            continue
        joined = (pending + line).strip()
        pending = ''
        if joined and not joined.startswith('#'):
            out.append(joined)
    return tuple(out)


@dataclass(frozen=True)
class Plan:
    install: tuple[str, ...]
    substitutions: tuple[tuple[str, str], ...]
    exported: tuple[str, ...]
    commands: tuple[str, ...]


def plan(text: str, wheel: str) -> Plan:
    found = [block for block in blocks(text) if block.lang in _SHELL]
    install_blocks = [
        block
        for block in found
        if block.section == 'Install' and any('pip install' in line for line in block.lines)
    ]
    if len(install_blocks) != 1:
        raise ReadmeError(
            '期望: README 的「## Install」一节恰好有一个带 pip install 的 shell 块。'
            f' 实际: 找到 {len(install_blocks)} 个。'
            ' 下一步: 改 README 或改 scripts/acceptance_readme.py 取块的规则。'
        )
    install: list[str] = []
    substitutions: list[tuple[str, str]] = []
    for line in install_blocks[0].lines:
        replaced: list[str] = []
        for token in shlex.split(line, posix=False):
            match = _PACKAGE.match(token)
            if match is None:
                replaced.append(token)
                continue
            source = shlex.quote(f'{wheel}{match.group("extras") or ""}')
            substitutions.append((token, source))
            replaced.append(source)
        install.append(' '.join(replaced))
    if len(substitutions) != 1:
        raise ReadmeError(
            '期望: 安装命令里恰好一处包来源 syndica[...]（换成 wheel）。'
            f' 实际: {len(substitutions)} 处：{substitutions}。'
            ' 下一步: 改 README 的安装命令，或改替换规则。'
        )
    exported = [
        token.partition('=')[0]
        for block in found
        if block.section == 'Quickstart' and block.executed is None
        for line in block.lines
        if line.startswith('export ')
        for token in shlex.split(line)[1:]
    ]
    commands = [
        line.replace(_TRACE_ID, _FIRST_TRACE)
        for block in found
        if block.executed == 'quickstart'
        for line in block.lines
    ]
    if not exported or not commands:
        raise ReadmeError(
            '期望: README 的「## Quickstart」有 export 块与标了 executed 的块。'
            f' 实际: export 的变量 {exported}，命令 {commands}。'
            ' 下一步: 对照 tests/test_docs.py 的约定改 README。'
        )
    return Plan(tuple(install), tuple(substitutions), tuple(exported), tuple(commands))


_PROLOGUE = """\
#!/bin/sh
# 由 scripts/acceptance_readme.py 从 README.md 生成。每条命令的输出存进 $out/<序号>.out / .err，
# 退出码与秒数存进 $out/codes.tsv。
set -eu
out=/accept/out
mkdir -p "$out"
n=0
now() {
  python3 -c 'import time; print(f"{time.time():.2f}")'
}
first_trace_id() {
  syndica trace list | sed -n '1s/^[^ ]* [^ ]*  \\([0-9a-f]\\{8\\}\\).*/\\1/p'
}
step() {
  n=$((n + 1))
  printf '%s\\t%s\\n' "$n" "$1" >>"$out/steps.tsv"
  echo "\\$ $1"
  began=$(now)
  code=0
  eval "$1" >"$out/$n.out" 2>"$out/$n.err" || code=$?
  ended=$(now)
  cat "$out/$n.out" "$out/$n.err"
  printf '%s\\t%s\\t%s %s\\n' "$n" "$code" "$began" "$ended" >>"$out/codes.tsv"
  return "$code"
}
"""


def script(steps: Plan, environment: dict[str, str] | None, from_stdin: list[str]) -> str:
    """`environment` 为 None 时（真模型模式）不用 README export 的占位：`from_stdin` 里的变量
    从 stdin 逐行读，值不写进脚本。"""
    lines = [_PROLOGUE]
    lines.append('# 夹具：脚本化的假 OpenAI 端点（不属于产品）。')
    lines.append('if [ -f /accept/fake_openai.py ]; then')
    lines.append(
        '  FAKE_OPENAI_SCRIPT=/accept/quickstart.json python3 /accept/fake_openai.py 8765 '
        '>/accept/out/fake.port 2>/accept/out/fake.log &'
    )
    lines.append('  until [ -s /accept/out/fake.port ]; do sleep 0.1; done')
    lines.append('fi')
    lines.append('cd "$HOME"')
    lines.append('# README「Install」，包来源换成拷进容器的 wheel：')
    lines += [f'step {shlex.quote(line)}' for line in steps.install]
    lines.append('# README「Quickstart」export 的变量，值是这次要用的端点：')
    if environment is None:
        for name in from_stdin:
            lines.append(f'IFS= read -r {name}')
            lines.append(f'export {name}')
    else:
        lines += [f'export {name}={shlex.quote(environment[name])}' for name in steps.exported]
    lines.append('# README 里标了 executed 的块，逐字：')
    for command in steps.commands:
        if command.startswith('cd '):
            lines.append(command)
        else:
            lines.append(f'step {shlex.quote(command)}')
    lines.append('cp -R . /accept/out/workspace')
    return '\n'.join(lines) + '\n'


def main(argv: list[str]) -> int:
    if len(argv) not in (4, 6) or (len(argv) == 6 and argv[4] != '--from-stdin'):
        print(
            '用法: acceptance_readme.py README.md <wheel> <输出脚本> [--from-stdin 变量,变量]',
            file=sys.stderr,
        )
        return 2
    readme, wheel, output = Path(argv[1]), argv[2], Path(argv[3])
    try:
        steps = plan(readme.read_text(encoding='utf-8'), wheel)
    except ReadmeError as error:
        print(str(error), file=sys.stderr)
        return 1
    from_stdin = argv[5].split(',') if len(argv) == 6 else []
    if from_stdin:
        environment = None
    else:
        environment = {
            'OPENAI_BASE_URL': 'http://127.0.0.1:8765/v1',
            'OPENAI_MODEL': 'fake',
            'OPENAI_API_KEY': 'acceptance-dummy-key-0123456789',
        }
        unknown = sorted(set(steps.exported) - set(environment))
        if unknown:
            print(
                f'期望: README export 的变量都有夹具端点下的取值 {sorted(environment)}。'
                f' 实际: 还有 {unknown}。 下一步: 在 scripts/acceptance_readme.py 里给它们取值。',
                file=sys.stderr,
            )
            return 1
    output.write_text(script(steps, environment, from_stdin), encoding='utf-8')
    print(
        json.dumps(
            {
                'install': steps.install,
                'substitutions': steps.substitutions,
                'exported': steps.exported,
                'commands': steps.commands,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
