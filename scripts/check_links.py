"""Markdown 相对链接必须指向存在的文件或目录（spec D18）。

查 `[文本](目标)`、`![图](目标)` 与引用定义 `[名]: 目标`，锚点与查询串不查；外链、纯锚点、围栏
代码块、行内代码与 HTML 注释里的链接跳过，测试夹具的数据目录不扫。行内代码必须跳过：文档里用反引号
写出来的示例链接不是链接。按段落而不是按行找：行内代码与链接都可以跨行。

目标要与别人克隆下来的文件逐字对得上：在 git 工作区里只认 `git ls-files` 列出、还在磁盘上的文件
（被忽略的文件在别人的克隆里不存在）；大小写逐段比对（macOS 不分大小写，GitHub 分）；不许经 `..`
跑出 --root。

没有断链时什么都不打、退出 0；有断链时逐条报 `文件:行号`，末行 `共 N 处断链。`、退出 1；根目录不对、
文件读不了时退出 2。检查结果解析器（`syndica.cli.checks`）按这套约定读它。

认不出的写法：缩进代码块（与列表项里缩进的续行段落分不开，当成正文查）；列表项里没关上的围栏（GitHub
在列表项结束时替它关上，这里一直算到文件末尾）。
"""

from __future__ import annotations

import argparse
import bisect
import html
import io
import os
import posixpath
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

# 这些目录里不是本仓的文档：版本库、虚拟环境、缓存、运行产物，以及测试夹具的数据。
SKIPPED_DIRECTORIES = frozenset(
    {
        '.git',
        '.mypy_cache',
        '.pytest_cache',
        '.ruff_cache',
        '.syndica',
        '.venv',
        '__pycache__',
        'fixtures',
        'node_modules',
    }
)
_MARKDOWN_SUFFIXES = ('.md', '.markdown')
# 围栏可以缩进任意深（列表项里的围栏）、可以在引用块里；反引号围栏的信息串里不能有反引号
# （```` ```uv run pytest``` ```` 是行内代码），关闭行不能带信息串。
_QUOTE = r'(?:[ \t]*>)*[ \t]*'
_FENCE_OPEN = re.compile(rf'{_QUOTE}(?:(`{{3,}})[^`]*|(~{{3,}}).*)')
_FENCE_CLOSE = re.compile(rf'{_QUOTE}(`{{3,}}|~{{3,}})[ \t]*')
_PARAGRAPH = re.compile(r'(?:[^\n]|\n(?![ \t]*(?:\n|\Z)))+')
_BACKTICKS = re.compile(r'`+')
# 链接文本可以嵌一层方括号：`[![徽章](图)](外链)` 里的图从文本里再找一遍。每处重复的各个分支首字符
# 互斥，且都停在下一个收尾字符（方括号、圆括号、引号、空白）上：没有收尾的一长串不会让每个起点都扫到
# 段落末尾。目标里的圆括号只认成对的一层。
_LINK = re.compile(
    r'(?<!\\)!?\[(?P<text>(?:\\.|[^\[\]\\]|\[(?:\\.|[^\[\]\\])*\])*)\]'
    r'\(\s*(?P<target><(?:\\.|[^<>\\\n])*>|(?:\\.|[^\s()\\]|\((?:\\.|[^\s()\\])*\))+)'
    r'(?:\s+(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\((?:\\.|[^()\\])*\)))?\s*\)'
)
_DEFINITION = re.compile(
    r'^[ ]{0,3}\[(?:\\.|[^\[\]\\])+\]:[ \t]*\n?[ \t]*(?P<target><(?:\\.|[^<>\\\n])*>|\S+)',
    re.MULTILINE,
)
_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:')
_ESCAPED = re.compile(r'\\([!-/:-@\[-`{-~])')


class CheckError(Exception):
    pass


def _markdown_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d not in SKIPPED_DIRECTORIES)
        for name in sorted(names):
            path = Path(directory) / name
            # 悬空的符号链接、FIFO 不是文档：读 FIFO 会一直卡住。
            if name.lower().endswith(_MARKDOWN_SUFFIXES) and path.is_file():
                found.append(path)
    return found


def _read(path: Path, relative: str) -> str:
    try:
        return path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError as exc:
        raise CheckError(
            f'期望 {relative} 是 UTF-8 编码的 Markdown，实际从第 {exc.start} 个字节起解不出来。'
            ' 把它转成 UTF-8；它若不是文档（比如 macOS 的 `._` 附属文件），删掉它。'
        ) from exc
    except OSError as exc:
        raise CheckError(
            f'期望能读 {relative}，实际读不了：{exc.strerror}。确认它是可读的普通文件，或者删掉它。'
        ) from exc


def _without_fences(text: str) -> str:
    """围栏代码块的每一行（含开、关两行）换成空行，行号不变。"""
    lines = text.split('\n')
    fence: str | None = None
    for index, line in enumerate(lines):
        if fence is not None:
            closed = _FENCE_CLOSE.fullmatch(line)
            if closed and closed.group(1)[0] == fence[0] and len(closed.group(1)) >= len(fence):
                fence = None
            lines[index] = ''
            continue
        opened = _FENCE_OPEN.fullmatch(line)
        if opened:
            fence = opened.group(1) or opened.group(2)
            lines[index] = ''
    return '\n'.join(lines)


def _code_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """一个段落里的行内代码：一串 n 个反引号开头，到下一串恰好 n 个反引号为止。按长度二分找收尾，
    一长串没有收尾的反引号也是线性的。被转义的反引号不开头（行内代码里没有转义，收尾不看这个）。"""
    runs = [(match.start(), match.end()) for match in _BACKTICKS.finditer(text, start, end)]
    by_length: dict[int, list[int]] = {}
    for index, (first, last) in enumerate(runs):
        by_length.setdefault(last - first, []).append(index)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        first, last = runs[index]
        backslashes = 0
        while first - backslashes > start and text[first - backslashes - 1] == '\\':
            backslashes += 1
        if backslashes % 2:
            first += 1
        same = by_length.get(last - first, [])
        following = bisect.bisect_right(same, index)
        if first < last and following < len(same):
            spans.append((first, runs[same[following]][1]))
            index = same[following] + 1
        else:
            index += 1
    return spans


def _blanked(text: str, spans: list[tuple[int, int]]) -> str:
    """把这些区间换成等长的空格（换行保留）：删掉会让两边拼出一条本来没有的链接，也会错开行号。"""
    pieces: list[str] = []
    position = 0
    for start, end in spans:
        pieces.append(text[position:start])
        pieces.append(re.sub(r'[^\n]', ' ', text[start:end]))
        position = end
    pieces.append(text[position:])
    return ''.join(pieces)


def _comments(text: str) -> list[tuple[int, int]]:
    """HTML 注释：`<!--` 到下一个 `-->`，没有收尾就到末尾。"""
    spans: list[tuple[int, int]] = []
    start = text.find('<!--')
    while start != -1:
        end = text.find('-->', start + 4)
        if end == -1:
            spans.append((start, len(text)))
            break
        spans.append((start, end + 3))
        start = text.find('<!--', end + 3)
    return spans


def _links(text: str, start: int, end: int) -> Iterator[tuple[int, str]]:
    for match in _LINK.finditer(text, start, end):
        yield match.start(), match.group('target')
        yield from _links(text, match.start('text'), match.end('text'))


def targets(text: str) -> list[tuple[int, str]]:
    """一份文档里要查的链接：(行号, 原样写着的目标)。"""
    visible = _without_fences(text)
    paragraphs = [(match.start(), match.end()) for match in _PARAGRAPH.finditer(visible)]
    code = [span for start, end in paragraphs for span in _code_spans(visible, start, end)]
    visible = _blanked(visible, code)
    visible = _blanked(visible, _comments(visible))
    newlines = [match.start() for match in re.finditer('\n', visible)]
    found: list[tuple[int, str]] = []
    for start, end in paragraphs:
        hits = list(_links(visible, start, end))
        hits += [(m.start(), m.group('target')) for m in _DEFINITION.finditer(visible, start, end)]
        for offset, raw in sorted(hits):
            found.append((bisect.bisect_left(newlines, offset) + 1, raw))
    return found


def _path(raw: str) -> str | None:
    """目标里的文件路径：去掉尖括号、转义、实体、锚点与查询串，再解百分号编码。外链与纯锚点是
    None。"""
    target = raw[1:-1] if raw.startswith('<') else raw
    if _SCHEME.match(target) or target.startswith('//'):
        return None
    target = html.unescape(_ESCAPED.sub(r'\1', target))
    path = unquote(re.split(r'[?#]', target, maxsplit=1)[0])
    return path or None


def _git_files(root: Path) -> set[str] | None:
    """git 工作区里进了版本库或还没跟踪、但没被忽略的文件（相对 root）；不在工作区里是 None。"""
    try:
        completed = subprocess.run(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    names = completed.stdout.decode('utf-8', 'surrogateescape').split('\0')
    return {name for name in names if name}


class _Files:
    def __init__(self, root: Path) -> None:
        self._root = root
        listed = _git_files(root)
        self._in_git = listed is not None
        self._known: set[str] = set(listed or ())
        for name in listed or ():
            parent = posixpath.dirname(name)
            while parent and parent not in self._known:
                self._known.add(parent)
                parent = posixpath.dirname(parent)
        self._folded: dict[str, str] = {}
        for name in sorted(self._known, reverse=True):
            self._folded[name.casefold()] = name
        self._entries: dict[Path, list[str]] = {}

    def problem(self, document: str, path: str) -> str | None:
        """这条链接的毛病（一整句，含下一步做什么）；没有毛病是 None。`document` 相对 root。"""
        if path.startswith('/'):
            relative = posixpath.normpath(path.lstrip('/') or '.')
        else:
            relative = posixpath.normpath(posixpath.join(posixpath.dirname(document), path))
        if relative == '..' or relative.startswith('../'):
            return (
                '经 `..` 跑出了 --root，别人的克隆里没有它。改成仓库里的文件，或者换成完整的 URL。'
            )
        if relative == '.':
            return None
        found, actual = self._lookup(relative)
        if found:
            return None
        if actual is not None:
            return f'大小写对不上：仓库里是 {actual!r}（macOS 不分大小写，GitHub 分）。照它改。'
        if self._in_git and os.path.lexists(self._root / relative):
            return (
                '被 .gitignore 忽略，别人的克隆里没有它。提交它、换一个进了版本库的文件，'
                '或者删掉这条链接。'
            )
        if path.startswith('/'):
            return '不存在。以 / 开头的路径从 --root 算起：改成存在的路径，或者删掉这条链接。'
        return '不存在。改成存在的相对路径（相对这份文档所在的目录），或者删掉这条链接。'

    def _lookup(self, relative: str) -> tuple[bool, str | None]:
        """(在不在, 只差大小写时仓库里的那个路径)。"""
        if self._in_git:
            if relative in self._known:
                return os.path.lexists(self._root / relative), None
            return False, self._folded.get(relative.casefold())
        parts = relative.split('/')
        current = self._root
        for index, part in enumerate(parts):
            entries = self._listing(current)
            if part not in entries:
                folded = part.casefold()
                match = next((entry for entry in entries if entry.casefold() == folded), None)
                if match is None:
                    return False, None
                return False, '/'.join([*parts[:index], match, *parts[index + 1 :]])
            current = current / part
        return True, None

    def _listing(self, directory: Path) -> list[str]:
        if directory not in self._entries:
            try:
                self._entries[directory] = sorted(os.listdir(directory))
            except (FileNotFoundError, NotADirectoryError):
                self._entries[directory] = []
            except OSError as exc:
                raise CheckError(
                    f'期望能列出 {directory} 里的文件来核对链接，实际列不了：{exc.strerror}。'
                    ' 给这个目录加上读与执行权限后再跑。'
                ) from exc
        return self._entries[directory]


def check(root: Path) -> list[str]:
    if not root.is_dir():
        raise CheckError(
            f'期望 --root 指向存在的目录，实际收到 {str(root)!r}：路径不存在或不是目录。'
            ' 请从仓库根目录运行，或把 --root 指向要检查的目录。'
        )
    files = _Files(root)
    broken: list[str] = []
    for document in _markdown_files(root):
        relative = document.relative_to(root).as_posix()
        for number, raw in targets(_read(document, relative)):
            path = _path(raw)
            problem = None if path is None else files.problem(relative, path)
            if problem is not None:
                shown = raw[1:-1] if raw.startswith('<') else raw
                broken.append(f'{relative}:{number}: 链接指向的 {shown!r} {problem}')
    return broken


def main() -> int:
    # 读这份输出的是检查结果解析器，它按 UTF-8 认 `共 N 处断链。`：严格的 C locale 下 stdout 默认是
    # ASCII，中文报告会让一次有结论的检查崩掉。解不开的文件名（代理字符）转义输出。
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.', help='要检查的目录，默认当前目录')
    args = parser.parse_args()
    try:
        broken = check(Path(args.root))
    except CheckError as exc:
        print(f'链接检查无法执行：{exc}', file=sys.stderr)
        return 2
    for line in broken:
        print(line)
    if broken:
        print(f'\n共 {len(broken)} 处断链。')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
