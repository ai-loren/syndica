"""`syndica/cli` 里的写文件调用只有两类（spec D12）：不带内容的，与内容是常量或脱敏器输出的。

handoff、提交消息与 `--handoff` 的两条流都可能带着密钥，落盘之前必须先过脱敏；多写一处不经脱敏的
文件，什么都不会报错，所以扫描而不是靠约定。写进磁盘只许经 `write_text` / `write_bytes` /
`os.write`，内容逐个看；写模式的 `open`、`json.dump`、`shutil.copy*` 这类看不见内容的写法一律不许。
于是 `cli` 里没有可写的文件句柄，剩下的 `.write(...)` 只能是 stdout / stderr，`--handoff` 下由脱敏
写入器包住。

子进程同样是写入点：提交消息经 `git commit -m` 进历史、经 `input=` 进 stdin，所以列表字面量里
`-m` / `--message` 后面那一项与 `input=` 按内容看；`stdout=` / `stderr=` 只许管道、丢弃与合流；
不许 shell（`shell=True`、`create_subprocess_shell`、`os.system`），重定向写什么看不见。

认不出的写法：把写函数、写方法或 `open` 赋给别的名字再调（`w = p.write_text`、`getattr`）；同名
局部变量遮住模块常量；参数列表不是字面量、而是一项项 `append` 出来的；标准库以外的写法。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[2] / 'src' / 'syndica'
_CLI = _PACKAGE / 'cli'
# 目前唯一带内容的写：本次 run 的 git 配置。守卫必须证明自己看过它。
_MUST_SCAN = 'cli/environment.py'

_CONTENT_METHODS = frozenset({'write_text', 'write_bytes'})
_BANNED_FUNCTIONS: dict[str, frozenset[str]] = {
    'asyncio': frozenset({'create_subprocess_shell'}),
    'bz2': frozenset({'BZ2File', 'open'}),
    'codecs': frozenset({'open'}),
    'gzip': frozenset({'GzipFile', 'open'}),
    'io': frozenset({'FileIO'}),
    'json': frozenset({'dump'}),
    'logging': frozenset({'FileHandler'}),
    'logging.handlers': frozenset(
        {'RotatingFileHandler', 'TimedRotatingFileHandler', 'WatchedFileHandler'}
    ),
    'lzma': frozenset({'LZMAFile', 'open'}),
    'marshal': frozenset({'dump'}),
    'os': frozenset(
        {
            'copy_file_range',
            'dup2',
            'fdopen',
            'popen',
            'pwrite',
            'pwritev',
            'sendfile',
            'splice',
            'system',
            'writev',
        }
    ),
    'pickle': frozenset({'dump'}),
    'shutil': frozenset({'copy', 'copy2', 'copyfile', 'copyfileobj', 'copytree'}),
    'subprocess': frozenset({'getoutput', 'getstatusoutput'}),
    'tarfile': frozenset({'TarFile', 'open'}),
    'tempfile': frozenset({'NamedTemporaryFile', 'SpooledTemporaryFile', 'TemporaryFile'}),
    'zipfile': frozenset({'ZipFile'}),
}
_OPEN_FUNCTIONS = frozenset({('io', 'open'), ('builtins', 'open')})
_SUBPROCESS: dict[str, frozenset[str]] = {
    'asyncio': frozenset({'create_subprocess_exec'}),
    'subprocess': frozenset({'Popen', 'call', 'check_call', 'check_output', 'run'}),
}
_STREAM_TARGETS = frozenset({'DEVNULL', 'PIPE', 'STDOUT'})
_MESSAGE_FLAGS = frozenset({'-m', '--message'})
_GLUED_MESSAGE = ('-m', '--message=')
_WRITE_MODE_LETTERS = frozenset('wax+')
_MODE_LETTERS = frozenset('rwxabtU+')


def _dotted(node: ast.expr) -> str | None:
    """`a`、`a.b.c` 这种由名字与属性组成的表达式的点分写法；别的表达式是 None。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted(node.value)
        return None if owner is None else f'{owner}.{node.attr}'
    return None


class _Checker:
    def __init__(self, tree: ast.Module) -> None:
        self.modules: dict[str, str] = {}
        self.imported: dict[str, tuple[str, str]] = {}
        self.constants: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign | ast.AnnAssign) and _is_literal(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                self.constants.update(t.id for t in targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
                    else:
                        # `import logging.handlers` 绑定的名字是 `logging`。
                        top = alias.name.split('.')[0]
                        self.modules[top] = top
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.imported[alias.asname or alias.name] = (node.module, alias.name)

    def resolve(self, func: ast.expr) -> tuple[str, str] | None:
        """调用目标的 (模块, 名字)：`module.name`、`package.module.name` 与
        `from module import name`。"""
        if isinstance(func, ast.Name):
            return self.imported.get(func.id)
        if not isinstance(func, ast.Attribute):
            return None
        owner = _dotted(func.value)
        if owner is None:
            return None
        head, _, rest = owner.partition('.')
        if head in self.modules:
            base = self.modules[head]
        elif head in self.imported:
            base = '.'.join(self.imported[head])
        else:
            return None
        return (f'{base}.{rest}' if rest else base), func.attr

    def banned_function(self, call: ast.Call) -> str | None:
        target = self.resolve(call.func)
        if target is None:
            return None
        module, name = target
        if name in _BANNED_FUNCTIONS.get(module, ()):
            return f'{module}.{name}'
        # 不带文件名的 `basicConfig` 只配一个写 stderr 的 handler。
        if target == ('logging', 'basicConfig') and (
            _keyword(call, 'filename') is not None or _has_double_star(call)
        ):
            return 'logging.basicConfig'
        return None

    def is_builtin_open(self, func: ast.expr) -> bool:
        """内置 `open`、`io.open` 或 `builtins.open`：模式是第二个参数。"""
        if isinstance(func, ast.Name) and func.id == 'open':
            return True
        return self.resolve(func) in _OPEN_FUNCTIONS

    def is_os_write(self, func: ast.expr) -> bool:
        return self.resolve(func) == ('os', 'write')

    def is_subprocess(self, func: ast.expr) -> bool:
        target = self.resolve(func)
        return target is not None and target[1] in _SUBPROCESS.get(target[0], ())

    def stream_target(self, node: ast.expr) -> bool:
        """`stdout=` / `stderr=` 的取值是管道、丢弃、合流或 None。"""
        if isinstance(node, ast.Constant):
            return node.value is None
        if isinstance(node, ast.Attribute):
            return node.attr in _STREAM_TARGETS
        return isinstance(node, ast.Name) and self.imported.get(node.id) in {
            ('subprocess', target) for target in _STREAM_TARGETS
        }

    def subprocess_violations(self, call: ast.Call) -> list[str]:
        found: list[str] = []
        shell = _keyword(call, 'shell')
        if shell is not None and not (isinstance(shell, ast.Constant) and shell.value is False):
            found.append('subprocess shell')
        if _keyword(call, 'input') is not None and not self.safe_content(_keyword(call, 'input')):
            found.append('subprocess input')
        for stream in ('stdout', 'stderr'):
            target = _keyword(call, stream)
            if target is not None and not self.stream_target(target):
                found.append(f'subprocess {stream}')
        if _has_double_star(call):
            found.append('subprocess **kwargs')
        return found

    def message_violations(self, elements: list[ast.expr]) -> list[str]:
        """参数列表字面量里 `-m` / `--message` 后面那一项，与粘在一起的 `-m{x}` /
        `--message={x}`。"""
        found: list[str] = []
        previous: ast.expr | None = None
        for element in elements:
            flag = isinstance(previous, ast.Constant) and previous.value in _MESSAGE_FLAGS
            if (flag and not self.safe_content(element)) or _glued_message(element):
                found.append('-m')
            previous = element
        return found

    def safe_content(self, node: ast.expr | None) -> bool:
        """常量，或脱敏器调用的直接结果（可以再 `.encode(...)`）。"""
        if node is None:
            return False
        if _is_literal(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in self.constants
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == 'encode':
                return self.safe_content(func.value)
            if isinstance(func, ast.Attribute):
                return func.attr == 'redact'
            return isinstance(func, ast.Name) and func.id == 'redact'
        return False


def _is_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _has_double_star(call: ast.Call) -> bool:
    return any(k.arg is None for k in call.keywords)


def _glued_message(node: ast.expr) -> bool:
    """`f'-m{x}'`、`'--message=' + x`：git 认粘在一起的写法，内容看不见。"""
    joined = False
    while isinstance(node, ast.BinOp):
        node, joined = node.left, True
    if isinstance(node, ast.JoinedStr) and node.values:
        joined = joined or any(isinstance(part, ast.FormattedValue) for part in node.values)
        node = node.values[0]
    return joined and isinstance(node, ast.Constant) and str(node.value).startswith(_GLUED_MESSAGE)


def _mode_like(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and set(node.value) <= _MODE_LETTERS
    )


def _opens_for_writing(call: ast.Call, *, method: bool) -> bool:
    """模式写或追加、或者看不出是什么模式。内置 `open` 的模式是第二个参数；`Path.open` 的是第一个，
    别的对象上的 `.open(name, mode)` 是第二个，两个位置都看。"""
    if _has_double_star(call) or any(isinstance(arg, ast.Starred) for arg in call.args):
        return True
    mode = _keyword(call, 'mode')
    if mode is None:
        candidates = call.args[:2] if method else call.args[1:2]
        mode = next((arg for arg in candidates if _mode_like(arg)), None)
        if mode is None:
            # `webbrowser.open("https://…")`：参数都是常量、没有一个像模式，就不是在开文件。
            if method:
                return not all(isinstance(arg, ast.Constant) for arg in candidates)
            return bool(candidates)
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(set(mode.value) & _WRITE_MODE_LETTERS)
    return True


def _violations(source: str) -> list[str]:
    tree = ast.parse(source)
    checker = _Checker(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple):
            found += checker.message_violations(node.elts)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        banned = checker.banned_function(node)
        if checker.is_subprocess(func):
            found += checker.subprocess_violations(node)
            # `create_subprocess_exec` 的参数本身就是参数列表，不是一个列表字面量。
            if checker.resolve(func) == ('asyncio', 'create_subprocess_exec'):
                found += checker.message_violations(list(node.args))
        if banned is not None:
            found.append(banned)
        elif isinstance(func, ast.Attribute) and func.attr in _CONTENT_METHODS:
            content = node.args[0] if node.args else _keyword(node, 'data')
            if not checker.safe_content(content):
                found.append(func.attr)
        elif checker.is_os_write(func):
            content = node.args[1] if len(node.args) > 1 else _keyword(node, 'data')
            if not checker.safe_content(content):
                found.append('os.write')
        elif checker.is_builtin_open(func):
            if _opens_for_writing(node, method=False):
                found.append('open')
        # `os.open` 交回的是 fd：往里写只能经 `os.write`（按内容看）或上面禁掉的那几种。
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == 'open'
            and checker.resolve(func) != ('os', 'open')
        ):
            if _opens_for_writing(node, method=True):
                found.append('.open')
    return found


def _scan(cli: Path = _CLI) -> tuple[dict[str, list[str]], list[str]]:
    """(违规的文件 → 违规写法, 本次真的扫过的文件)，路径相对 cli 的上一级。"""
    violations: dict[str, list[str]] = {}
    scanned: list[str] = []
    for path in sorted(cli.rglob('*.py')):
        relative = path.relative_to(cli.parent).as_posix()
        scanned.append(relative)
        found = _violations(path.read_text(encoding='utf-8'))
        if found:
            violations[relative] = found
    return violations, scanned


def _cli_python_files() -> set[str]:
    """cli 里全部 `.py`，用 `os.walk` 独立走一遍：范围被收窄时两份清单只有同时改才对得上。"""
    found: set[str] = set()
    for root, _, names in os.walk(_CLI):
        for name in names:
            if name.endswith('.py'):
                found.add((Path(root) / name).relative_to(_PACKAGE).as_posix())
    return found


def test_every_file_write_in_the_cli_is_content_free_constant_or_redacted():
    violations, _ = _scan()

    assert violations == {}, (
        f'期望 src/syndica/cli 写进磁盘的内容只有常量或脱敏器的输出；实际 {violations}。'
        ' 把内容先交给 Redactor.redact，再经 write_text / write_bytes / os.write 写，调用写在同一个'
        '表达式里，守卫才看得出它过了脱敏。'
    )


def test_the_scan_really_covered_the_file_that_writes_content():
    _, scanned = _scan()

    assert _MUST_SCAN in scanned


def test_the_scan_covers_the_whole_cli_package():
    _, scanned = _scan()

    assert set(scanned) == _cli_python_files()


def test_a_violation_planted_in_the_environment_module_is_reported(tmp_path: Path):
    cli = tmp_path / 'cli'
    cli.mkdir()
    (cli / 'environment.py').write_text('def f(p, s):\n    p.write_text(s)\n', encoding='utf-8')
    (cli / 'clean.py').write_text('X = 1\n', encoding='utf-8')

    violations, scanned = _scan(cli)

    assert violations == {_MUST_SCAN: ['write_text']}
    assert scanned == ['cli/clean.py', _MUST_SCAN]


def test_a_violation_planted_in_a_cli_subpackage_is_reported(tmp_path: Path):
    nested = tmp_path / 'cli' / 'dev'
    nested.mkdir(parents=True)
    (nested / 'log.py').write_text('def f(p, s):\n    p.write_bytes(s)\n', encoding='utf-8')

    violations, _ = _scan(tmp_path / 'cli')

    assert violations == {'cli/dev/log.py': ['write_bytes']}


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('p.write_text(text)', 'write_text'),
        ('p.write_bytes(data)', 'write_bytes'),
        ('p.write_text(data=text)', 'write_text'),
        ('p.write_text(f"{x}")', 'write_text'),
        ('p.write_text(redactor.redact)', 'write_text'),
        ('p.write_text(redactor.render(text))', 'write_text'),
        ('p.write_text(self.unredacted(text))', 'write_text'),
        ('p.write_bytes(secret.encode())', 'write_bytes'),
        ('def f():\n    X = "a"\n    X = secret\n    p.write_text(X)', 'write_text'),
        ('HEADER = compute()\np.write_text(HEADER)', 'write_text'),
        ('open(p, "w")', 'open'),
        ('open(p, "a")', 'open'),
        ('open(p, "x")', 'open'),
        ('open(p, "r+")', 'open'),
        ('open(p, mode="wb")', 'open'),
        ('open(p, mode)', 'open'),
        ('open(p, **options)', 'open'),
        ('open(*arguments)', 'open'),
        ('p.open("w")', '.open'),
        ('p.open("wb")', '.open'),
        ('p.open("r+")', '.open'),
        ('p.open(mode="a")', '.open'),
        ('p.open(mode)', '.open'),
        ('archive.open(name, "w")', '.open'),
        ('import io\nio.open(p, "w")', 'open'),
        ('from io import open as o\no(p, "a")', 'open'),
        ('import builtins\nbuiltins.open(p, "w")', 'open'),
        ('import os\nos.write(fd, data)', 'os.write'),
        ('from os import write\nwrite(fd, data)', 'os.write'),
        ('import os\nos.fdopen(fd, "w")', 'os.fdopen'),
        ('import os\nos.pwrite(fd, b"", 0)', 'os.pwrite'),
        ('import os\nos.writev(fd, [b""])', 'os.writev'),
        ('import json\njson.dump(x, f)', 'json.dump'),
        ('import pickle\npickle.dump(x, f)', 'pickle.dump'),
        ('import marshal\nmarshal.dump(x, f)', 'marshal.dump'),
        ('import codecs\ncodecs.open(p, "w")', 'codecs.open'),
        ('import shutil\nshutil.copy(a, b)', 'shutil.copy'),
        ('import shutil\nshutil.copy2(a, b)', 'shutil.copy2'),
        ('import shutil\nshutil.copyfileobj(a, b)', 'shutil.copyfileobj'),
        ('import shutil as sh\nsh.copytree(a, b)', 'shutil.copytree'),
        ('from shutil import copyfile\ncopyfile(a, b)', 'shutil.copyfile'),
        ('import tempfile\ntempfile.NamedTemporaryFile()', 'tempfile.NamedTemporaryFile'),
        ('import tempfile\ntempfile.TemporaryFile()', 'tempfile.TemporaryFile'),
        ('import tempfile\ntempfile.SpooledTemporaryFile()', 'tempfile.SpooledTemporaryFile'),
        ('from tempfile import NamedTemporaryFile as N\nN()', 'tempfile.NamedTemporaryFile'),
        ('import io\nio.FileIO(p, "w")', 'io.FileIO'),
        ('import gzip\ngzip.open(p, "wt")', 'gzip.open'),
        ('import gzip\ngzip.GzipFile(p, "w")', 'gzip.GzipFile'),
        ('import bz2\nbz2.open(p, "w")', 'bz2.open'),
        ('import bz2\nbz2.BZ2File(p, "w")', 'bz2.BZ2File'),
        ('import lzma\nlzma.open(p, "w")', 'lzma.open'),
        ('import lzma\nlzma.LZMAFile(p, "w")', 'lzma.LZMAFile'),
        ('import zipfile\nzipfile.ZipFile(p, "w")', 'zipfile.ZipFile'),
        ('import tarfile\ntarfile.open(p, "w")', 'tarfile.open'),
        ('import tarfile\ntarfile.TarFile(p, "w")', 'tarfile.TarFile'),
        ('import logging\nlogging.FileHandler(p)', 'logging.FileHandler'),
        ('import logging\nlogging.basicConfig(filename=p)', 'logging.basicConfig'),
        ('import logging\nlogging.basicConfig(**settings)', 'logging.basicConfig'),
        (
            'import logging.handlers\nlogging.handlers.TimedRotatingFileHandler(p)',
            'logging.handlers.TimedRotatingFileHandler',
        ),
        ('import os\nos.copy_file_range(src, out, n)', 'os.copy_file_range'),
        ('import os\nos.pwritev(fd, [b""], 0)', 'os.pwritev'),
        ('import os\nos.splice(src, out, n)', 'os.splice'),
        ('import subprocess\nsubprocess.getstatusoutput(command)', 'subprocess.getstatusoutput'),
        ('import subprocess\nsubprocess.call(argv, stdout=handle)', 'subprocess stdout'),
        ('import subprocess\nsubprocess.check_call(argv, input=message)', 'subprocess input'),
        ('import subprocess\nsubprocess.check_output(argv, shell=True)', 'subprocess shell'),
        ('import subprocess\nsubprocess.run(argv, stdout=self.log)', 'subprocess stdout'),
        (
            'import logging.handlers\nlogging.handlers.RotatingFileHandler(p)',
            'logging.handlers.RotatingFileHandler',
        ),
        (
            'from logging import handlers\nhandlers.WatchedFileHandler(p)',
            'logging.handlers.WatchedFileHandler',
        ),
        ('import os\nos.dup2(fd, 1)', 'os.dup2'),
        ('import os\nos.sendfile(out, src, 0, n)', 'os.sendfile'),
        ('import os\nos.system(command)', 'os.system'),
        ('import os\nos.popen(command)', 'os.popen'),
        ('import subprocess\nsubprocess.getoutput(command)', 'subprocess.getoutput'),
        (
            'import asyncio\nasyncio.create_subprocess_shell(command)',
            'asyncio.create_subprocess_shell',
        ),
        ('import subprocess\nsubprocess.run(["git", "commit", "-m", message])', '-m'),
        ('argv = ["git", "commit", "--message", message]', '-m'),
        ('argv = ("git", "commit", f"-m{message}")', '-m'),
        ('argv = ["git", "commit", "--message=" + message]', '-m'),
        (
            'import asyncio\nasyncio.create_subprocess_exec("git", "commit", "-m", message)',
            '-m',
        ),
        ('import subprocess\nsubprocess.run(argv, input=message)', 'subprocess input'),
        ('import subprocess\nsubprocess.run(argv, shell=True)', 'subprocess shell'),
        ('import subprocess\nsubprocess.run(argv, stdout=handle)', 'subprocess stdout'),
        ('from subprocess import Popen\nPopen(argv, stderr=fd)', 'subprocess stderr'),
        ('import subprocess\nsubprocess.run(argv, **options)', 'subprocess **kwargs'),
    ],
)
def test_the_guard_flags_every_write_whose_content_it_cannot_see(source: str, expected: str):
    assert expected in _violations(source)


@pytest.mark.parametrize(
    'source',
    [
        'p.write_text("*")',
        'p.write_text("")',
        'CONFIG = "[safe]\\n"\np.write_text(CONFIG)',
        'CONFIG: str = "x"\np.write_text(CONFIG, encoding="utf-8")',
        'p.write_text(redactor.redact(text))',
        'p.write_text(data=redactor.redact(text))',
        'p.write_text(redact(text))',
        'p.write_bytes(redactor.redact(text).encode())',
        'import os\nos.write(fd, redactor.redact(text).encode("utf-8"))',
        'open(p)',
        'open(p, "r")',
        'open(p, mode="rb")',
        'p.open()',
        'p.open("r")',
        'webbrowser.open("https://example.com")',
        'p.mkdir()',
        'p.touch()',
        'import tempfile\ntempfile.mkdtemp()',
        'import os\nos.open(p, os.O_RDONLY)',
        'import os\nos.replace(a, b)',
        'stream.write(text)',
        'x.copy()',
        'import json\njson.dumps(x)',
        'p.open(name, "r")',
        'import logging\nlogging.basicConfig(level=logging.INFO)',
        'import subprocess\nsubprocess.run(["git", "status"], stdout=subprocess.PIPE)',
        'import subprocess\nsubprocess.run(argv, stderr=subprocess.STDOUT, stdout=None)',
        'import asyncio\nasyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL)',
        'from subprocess import PIPE, run\nrun(argv, stdout=PIPE, shell=False)',
        'import subprocess\nsubprocess.run(argv, input=redactor.redact(message))',
        'import subprocess\nsubprocess.run(["git", "commit", "-m", redactor.redact(message)])',
        'argv = [sys.executable, "-m", "pytest", *extra]',
        'SUBJECT = "chore: sync"\nargv = ["git", "commit", "-m", SUBJECT]',
        'argv = ["git", "commit", f"-mchore", "--message=" "sync"]',
        'import os\nos.open(p, os.O_WRONLY | os.O_CREAT)',
    ],
)
def test_the_guard_allows_content_free_constant_and_redacted_writes(source: str):
    assert _violations(source) == []
