"""整个 `syndica` 包的写入点与外发点只有三类（spec D12、M3 D5）：不带内容的；内容是常量或脱敏器的
输出的；以及 `_ALLOWED` 里逐个文件写死、写明理由的那几处。

交接文件、提交消息、trace 与导出给第三方的数据都可能带着密钥，落盘或发出之前必须先过脱敏；多写一处
不经脱敏的文件，什么都不会报错，所以扫描而不是靠约定。写进磁盘只许经 `write_text` / `write_bytes`
/ `os.write` / `write_all`，内容逐个看；`RedactedText` 经 `write_redacted` 写，它在运行时检查类型
并写满为止（静态类型挡不住 `Any`，`os.write` 只调一次可能只写进一部分）。写模式的 `open`、
`json.dump`、`shutil.copy*`、套接字、网络日志 handler 这类看不见内容的写法一律不许。剩下的
`.write(...)` 只能是 stdout / stderr（`--handoff` 下由脱敏写入器包住），子进程的 stdin 另按内容看。

子进程同样是写入点：提交消息经 `git commit -m` 进历史、经 `input=` 进 stdin，所以列表字面量里
`-m`、`-am` 这类以 `m` 结尾的短选项与 `--message` 后面那一项、`input=` 按内容看；`stdout=` /
`stderr=` 只许管道、丢弃与合流；不许 shell（`shell=True`、`create_subprocess_shell`、`os.system`），
重定向写什么看不见。外发点同理：`.post` / `.put` / `.patch` / `.request` / `.stream` / `.send` /
`.build_request` / `Request(...)` / `urlopen` / `sendall` 的正文按内容看，不论按关键字还是按位置给，
也不论是直接调还是把方法交给 `partial` / `to_thread` 这类包装再调。

脱敏只有一个收口：`kernel/redaction.py`。别处定义或赋值 `redact` / `encode_redacted`，或者在类型
标注与 `isinstance` 之外用到 `RedactedText`（构造、继承、cast、取别名、当参数传）都算违规：否则另起
一个 `def redact(self, x): return x`，或把一段普通字符串变成 `RedactedText`，任何内容都能看起来过了
脱敏。

认不出的写法：把写函数、写方法或 `open` 赋给别的名字再调（`w = p.write_text`、`getattr`）；
`__getattr__` 交出的 `redact`；参数列表不是字面量、而是一项项 `append` 出来的；标准库以外的写法。
"""

from __future__ import annotations

import ast
import os
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[1] / 'src' / 'syndica'
_REDACTION_MODULE = 'kernel/redaction.py'
# 每个扫描根各一个必须扫到的文件，守卫要证明自己看过它们：本次 run 的 git 配置（唯一带内容的
# CLI 写入）、白名单里的三处、runner 与包根的入口。
_ANCHORS = (
    '__main__.py',
    'backends/llm_openai/client.py',
    'cli/environment.py',
    'kernel/local_workspace.py',
    'kernel/redaction.py',
    'runner/scenario.py',
)
# 刻意放行的写入与外发：按文件、按写法逐条写死，计数也算。白名单是文件而不是目录：同一目录里的新
# 文件、同一文件里多出来的一处都会变红。
_ALLOWED: dict[str, tuple[str, ...]] = {
    # 发给模型提供方的请求：对话本来就要发给提供方，这是 run 的前提，不是观测数据（M2 D5）。
    'backends/llm_openai/client.py': ('post json', 'post json'),
    # Agent 自己的文件工具：写进工作区的就是 run 的产物。命令的输出走自建的管道，读端经 `os.fdopen`
    # 接进事件循环，写端交给子进程。
    'kernel/local_workspace.py': (
        'os.fdopen',
        'subprocess stderr',
        'subprocess stdout',
        'write_bytes',
        'write_bytes',
    ),
    # 工作区路由把 Agent 文件工具的写转给它所在的那个工作区，内容就是工具交下来的。
    'kernel/workspace_router.py': ('write_bytes', 'write_text'),
    # Agent 自己的终端：写进伪终端主端的是 Agent 经 Terminal 交下来的输入，子进程的输出接在从端上。
    'kernel/local_pty.py': ('os.write', 'subprocess stderr', 'subprocess stdout'),
    # `write_all` 里那一处把短写接着写完的 `os.write`：写进去的内容在调用 `write_all` 的地方按内容
    # 看，或者由 `write_redacted` 在运行时查过类型。
    'kernel/redaction.py': ('os.write',),
    'kernel/tools/local.py': ('write_text',),
    # 信任存储：配置文件的绝对路径与它们的摘要，不含密钥；写在工作区之外（M4 D15）。
    'cli/trust.py': ('write_text',),
}

_CONTENT_METHODS = frozenset({'write_text', 'write_bytes'})
_BANNED_FUNCTIONS: dict[str, frozenset[str]] = {
    'asyncio': frozenset(
        {
            'create_subprocess_shell',
            'open_connection',
            'open_unix_connection',
            'start_server',
            'start_unix_server',
        }
    ),
    'bz2': frozenset({'BZ2File', 'open'}),
    'codecs': frozenset({'open'}),
    'gzip': frozenset({'GzipFile', 'open'}),
    'io': frozenset({'FileIO'}),
    'json': frozenset({'dump'}),
    'logging': frozenset({'FileHandler'}),
    'logging.config': frozenset({'dictConfig', 'fileConfig', 'listen'}),
    'logging.handlers': frozenset(
        {
            'DatagramHandler',
            'HTTPHandler',
            'RotatingFileHandler',
            'SMTPHandler',
            'SocketHandler',
            'SysLogHandler',
            'TimedRotatingFileHandler',
            'WatchedFileHandler',
        }
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
    'socket': frozenset({'create_connection', 'create_server', 'fromfd', 'socket', 'socketpair'}),
    'subprocess': frozenset({'getoutput', 'getstatusoutput'}),
    'tarfile': frozenset({'TarFile', 'open'}),
    'tempfile': frozenset({'NamedTemporaryFile', 'SpooledTemporaryFile', 'TemporaryFile'}),
    'urllib.request': frozenset({'urlretrieve'}),
    'zipfile': frozenset({'ZipFile'}),
}
_OPEN_FUNCTIONS = frozenset({('io', 'open'), ('builtins', 'open')})
_SUBPROCESS: dict[str, frozenset[str]] = {
    'asyncio': frozenset({'create_subprocess_exec'}),
    'subprocess': frozenset({'Popen', 'call', 'check_call', 'check_output', 'run'}),
}
_STREAM_TARGETS = frozenset({'DEVNULL', 'PIPE', 'STDOUT'})
# 以 `m` 结尾的短选项串（`-m`、`-am`、`-qm`）后面那一项是提交消息；粘在一起的写法同理。
_MESSAGE_FLAG = re.compile(r'-[A-Za-z]*m|--message')
_GLUED_MESSAGE = re.compile(r'-[A-Za-z]*m|--message=')
_WRITE_MODE_LETTERS = frozenset('wax+')
_MODE_LETTERS = frozenset('rwxabtU+')
# 外发调用的名字 → 从第几个位置参数起是正文（之前的是方法与地址）：`requests.post(url, data)`、
# `http.client` 的 `request(method, url, body)`、`urllib` 的 `Request(url, data)` 与
# `urlopen(url, data)`。
_OUTBOUND_BODY_FROM: dict[str, int] = {
    'Request': 1,
    'build_request': 2,
    'patch': 1,
    'post': 1,
    'put': 1,
    'request': 2,
    'sendall': 0,
    'sendmsg': 0,
    'sendto': 0,
    'stream': 2,
    'urlopen': 1,
}
# `.send(x)` 只有一个位置参数时是套接字或 websocket 的发送；`Runtime.send(to, content)` 有两个。
_OUTBOUND = frozenset({*_OUTBOUND_BODY_FROM, 'send'})
# 按位置给的正文只在认得出是 HTTP 的调用上看：`put(key, value)`、`post(item)` 这类名字在别处多得是。
_HTTP_MODULES = frozenset({'aiohttp', 'http', 'httpx', 'requests', 'urllib'})
_HTTP_METHODS = frozenset({'DELETE', 'GET', 'HEAD', 'OPTIONS', 'PATCH', 'POST', 'PUT'})
_SOCKET_SENDS = frozenset({'sendall', 'sendmsg', 'sendto'})
_OUTBOUND_BODIES = ('body', 'content', 'data', 'files', 'json')
_REDACTION_NAMES = frozenset({'encode_redacted', 'redact'})
_REDACTION_MODULES = frozenset({'syndica.kernel.redaction', 'syndica.kernel'})
# 只是改变表示、不改变内容的变换：它们的输出与输入同样安全。
_TRANSFORMS = frozenset({('gzip', 'compress'), ('zlib', 'compress')})


def _dotted(node: ast.expr) -> str | None:
    """`a`、`a.b.c` 这种由名字与属性组成的表达式的点分写法；别的表达式是 None。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted(node.value)
        return None if owner is None else f'{owner}.{node.attr}'
    return None


def _package_of(relative: str) -> list[str]:
    """包根下的文件所在的包，用来解析相对 import：`kernel/stores/x.py` → syndica.kernel.stores。"""
    if not relative:
        return ['syndica']
    return ['syndica', *Path(relative).with_suffix('').parts][:-1]


class _Checker:
    def __init__(self, tree: ast.Module, relative: str = '') -> None:
        self.redaction_module = relative == _REDACTION_MODULE
        self.modules: dict[str, str] = {}
        self.imported: dict[str, tuple[str, str]] = {}
        package = _package_of(relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
                    else:
                        # `import logging.handlers` 绑定的名字是 `logging`。
                        top = alias.name.split('.')[0]
                        self.modules[top] = top
            elif isinstance(node, ast.ImportFrom):
                base = package[: len(package) - node.level + 1] if node.level else []
                module = '.'.join([*base, *([node.module] if node.module else [])])
                for alias in node.names:
                    self.imported[alias.asname or alias.name] = (module, alias.name)
        # 模块级、绑定字面量、整个模块里只绑定过这一次的名字才是常量：在别处（`global` 之后、
        # `+=`）再绑定一次，写进去的就不再是那个字面量。
        bindings = Counter(
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        )
        self.constants: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign | ast.AnnAssign) and _is_literal(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                self.constants.update(
                    t.id for t in targets if isinstance(t, ast.Name) and bindings[t.id] == 1
                )
        # 类型标注与 `isinstance` / `issubclass` 的类型参数：`RedactedText` 只许出现在这些位置。
        self.typing_positions: set[int] = set()
        for node in ast.walk(tree):
            roots: list[ast.expr] = []
            if isinstance(node, ast.arg) and node.annotation is not None:
                roots.append(node.annotation)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
                roots.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                roots.append(node.annotation)
            elif (
                isinstance(node, ast.Call)
                and _callee_name(node.func) in {'isinstance', 'issubclass'}
                and len(node.args) == 2
            ):
                roots.append(node.args[1])
            for root in roots:
                self.typing_positions.update(id(child) for child in ast.walk(root))

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

    def from_redaction(self, func: ast.expr, name: str) -> bool:
        target = self.resolve(func)
        return target is not None and target[1] == name and target[0] in _REDACTION_MODULES

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
        """参数列表字面量里 `-m` / `-am` / `--message` 后面那一项，与粘在一起的 `-m{x}` /
        `--message={x}`。"""
        found: list[str] = []
        previous: ast.expr | None = None
        for element in elements:
            flag = (
                isinstance(previous, ast.Constant)
                and isinstance(previous.value, str)
                and _MESSAGE_FLAG.fullmatch(previous.value) is not None
            )
            if (flag and not self.safe_content(element)) or _glued_message(element):
                found.append('-m')
            previous = element
        return found

    def safe_content(self, node: ast.expr | None) -> bool:
        """常量，或脱敏器调用的直接结果（可以再 `.encode(...)`、`gzip.compress(...)`）。"""
        if node is None:
            return False
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Name):
            return node.id in self.constants
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'encode':
            return self.safe_content(func.value)
        if self.resolve(func) in _TRANSFORMS:
            return bool(node.args) and self.safe_content(node.args[0])
        if isinstance(func, ast.Name):
            return any(self.from_redaction(func, name) for name in _REDACTION_NAMES)
        if not isinstance(func, ast.Attribute) or func.attr not in _REDACTION_NAMES:
            return False
        # 对象上的方法（`redactor.redact(...)`）只能是内核的脱敏器：别处不许定义这两个名字。模块
        # 上的同名函数只认 `syndica.kernel.redaction` 的。
        target = self.resolve(func)
        return target is None or target[0] in _REDACTION_MODULES

    def outbound_violations(self, call: ast.Call) -> list[str]:
        """正文按关键字或按位置给；方法也可能先交给一个包装（`partial(client.post, url, json=x)`、
        `asyncio.to_thread(requests.post, ...)`），正文就在包装的调用上。"""
        name = _callee_name(call.func)
        if name in _OUTBOUND:
            label = name
            positional = self.positional_body(call, name)
            positional += [
                argument
                for argument in call.args
                if isinstance(argument, ast.Starred) and argument not in positional
            ]
        else:
            wrapped = next(
                (
                    (index, inner)
                    for index, argument in enumerate(call.args)
                    if (inner := self.method_reference(argument)) is not None
                ),
                None,
            )
            if wrapped is None:
                return []
            index, label = wrapped
            positional = list(call.args[index + 1 + _OUTBOUND_BODY_FROM.get(label, 0) :])
        found = [
            f'{label} {keyword.arg}'
            for keyword in call.keywords
            if keyword.arg in _OUTBOUND_BODIES and not self.safe_content(keyword.value)
        ]
        for argument in positional:
            if isinstance(argument, ast.Starred):
                found.append(f'{label} *args')
            elif not self.safe_content(argument):
                found.append(f'{label} positional')
        if _has_double_star(call):
            found.append(f'{label} **kwargs')
        return found

    def positional_body(self, call: ast.Call, name: str) -> list[ast.expr]:
        """按位置给的正文：HTTP 库的函数（`requests.post(url, data)`）、带字面量方法名的
        `.request("POST", url, body)`、套接字的发送与只有一个参数的 `.send(x)`。"""
        target = self.resolve(call.func)
        if target is not None and target[0].split('.')[0] in _HTTP_MODULES:
            return list(call.args[_OUTBOUND_BODY_FROM.get(name, 1) :])
        first = call.args[0] if call.args else None
        if (
            name == 'request'
            and isinstance(first, ast.Constant)
            and str(first.value).upper() in _HTTP_METHODS
        ):
            return list(call.args[2:])
        if name in _SOCKET_SENDS:
            return list(call.args[:1])
        return list(call.args) if name == 'send' and len(call.args) == 1 else []

    def method_reference(self, node: ast.expr) -> str | None:
        """交给包装的外发方法：`client.post` 这种属性，或从别处 import 进来的 `post`。一个恰好叫
        `request` 的局部变量不算。"""
        if isinstance(node, ast.Attribute) and node.attr in _OUTBOUND:
            return node.attr
        if isinstance(node, ast.Name) and node.id in self.imported:
            original = self.imported[node.id][1]
            return original if original in _OUTBOUND else None
        return None

    def redaction_violations(self, node: ast.AST) -> list[str]:
        """`kernel/redaction.py` 之外定义或赋值 `redact` / `encode_redacted`，或者在类型标注与
        `isinstance` 之外用到 `RedactedText`。"""
        if self.redaction_module:
            return []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return [f'def {node.name}'] if node.name in _REDACTION_NAMES else []
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            return [f'assign {name}' for name in _bound_names(targets) if name in _REDACTION_NAMES]
        if isinstance(node, ast.ImportFrom):
            return [
                'RedactedText alias'
                for alias in node.names
                if alias.name == 'RedactedText' and alias.asname not in (None, 'RedactedText')
            ]
        if (isinstance(node, ast.Name) and node.id == 'RedactedText') or (
            isinstance(node, ast.Attribute) and node.attr == 'RedactedText'
        ):
            if id(node) in self.typing_positions:
                return []
            return ['RedactedText outside an annotation']
        if not isinstance(node, ast.Call):
            return []
        callee = _callee_name(node.func)
        first = node.args[0] if node.args else None
        second = node.args[1] if len(node.args) > 1 else None
        if (
            callee == 'cast'
            and isinstance(first, ast.Constant)
            and 'RedactedText' in str(first.value)
        ):
            return ['cast(RedactedText)']
        if (
            callee == 'getattr'
            and isinstance(second, ast.Constant)
            and second.value == 'RedactedText'
        ):
            return ['RedactedText outside an annotation']
        found = [
            f'assign {keyword.arg}' for keyword in node.keywords if keyword.arg in _REDACTION_NAMES
        ]
        attribute = node.args[1] if callee in {'setattr', '__setattr__'} and second else None
        if isinstance(attribute, ast.Constant) and attribute.value in _REDACTION_NAMES:
            found.append(f'assign {attribute.value}')
        return found


def _is_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _bound_names(targets: Sequence[ast.expr]) -> list[str]:
    """赋值目标绑定的名字：`x`、`obj.x`，元组与星号里的逐个拆开。"""
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Tuple | ast.List):
            names += _bound_names(list(target.elts))
        elif isinstance(target, ast.Starred):
            names += _bound_names([target.value])
        elif isinstance(target, ast.Name | ast.Attribute):
            names.append(target.id if isinstance(target, ast.Name) else target.attr)
    return names


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _has_double_star(call: ast.Call) -> bool:
    return any(k.arg is None for k in call.keywords)


def _glued_message(node: ast.expr) -> bool:
    """`f'-m{x}'`、`f'-am{x}'`、`'--message=' + x`：git 认粘在一起的写法，内容看不见。"""
    joined = False
    while isinstance(node, ast.BinOp):
        node, joined = node.left, True
    if isinstance(node, ast.JoinedStr) and node.values:
        joined = joined or any(isinstance(part, ast.FormattedValue) for part in node.values)
        node = node.values[0]
    return (
        joined
        and isinstance(node, ast.Constant)
        and _GLUED_MESSAGE.match(str(node.value)) is not None
    )


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


def _violations(source: str, relative: str = '') -> list[str]:
    tree = ast.parse(source)
    checker = _Checker(tree, relative)
    found: list[str] = []
    for node in ast.walk(tree):
        found += checker.redaction_violations(node)
        if isinstance(node, ast.List | ast.Tuple):
            found += checker.message_violations(node.elts)
            continue
        if not isinstance(node, ast.Call):
            continue
        found += checker.outbound_violations(node)
        func = node.func
        banned = checker.banned_function(node)
        if isinstance(func, ast.Attribute) and func.attr == 'communicate':
            content = node.args[0] if node.args else _keyword(node, 'input')
            if content is not None and not checker.safe_content(content):
                found.append('communicate input')
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
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in {'write', 'writelines'}
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == 'stdin'
        ):
            if not checker.safe_content(node.args[0] if node.args else None):
                found.append('stdin write')
        elif checker.is_os_write(func) or checker.from_redaction(func, 'write_all'):
            content = node.args[1] if len(node.args) > 1 else _keyword(node, 'data')
            if not checker.safe_content(content):
                found.append('os.write' if checker.is_os_write(func) else 'write_all')
        elif checker.is_builtin_open(func):
            if _opens_for_writing(node, method=False):
                found.append('open')
        # `os.open` 交回的是 fd：往里写只能经 `os.write` / `write_all`（按内容看）、
        # `write_redacted`（运行时查类型）或上面禁掉的那几种。
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == 'open'
            and checker.resolve(func) != ('os', 'open')
        ):
            if _opens_for_writing(node, method=True):
                found.append('.open')
    return found


def _scan(package: Path = _PACKAGE) -> tuple[dict[str, list[str]], list[str]]:
    """(违规的文件 → 排好序的违规写法, 本次真的扫过的文件)，路径相对包根。"""
    violations: dict[str, list[str]] = {}
    scanned: list[str] = []
    for path in sorted(package.rglob('*.py')):
        relative = path.relative_to(package).as_posix()
        scanned.append(relative)
        found = _violations(path.read_text(encoding='utf-8'), relative)
        if found:
            violations[relative] = sorted(found)
    return violations, scanned


def _unexplained(violations: dict[str, list[str]]) -> dict[str, list[str]]:
    """白名单解释不了的违规：文件不在白名单里，或写法与次数和白名单不完全一致。"""
    return {path: found for path, found in violations.items() if tuple(found) != _ALLOWED.get(path)}


def _package_python_files() -> set[str]:
    """包里全部 `.py`，用 `os.walk` 独立走一遍：范围被收窄时两份清单只有同时改才对得上。"""
    found: set[str] = set()
    for root, _, names in os.walk(_PACKAGE):
        for name in names:
            if name.endswith('.py'):
                found.add((Path(root) / name).relative_to(_PACKAGE).as_posix())
    return found


def test_every_write_and_outbound_call_is_content_free_constant_redacted_or_allowed():
    violations, _ = _scan()

    assert _unexplained(violations) == {}, (
        f'期望 src/syndica 写进磁盘与发出去的内容只有常量或脱敏器的输出；'
        f'实际 {_unexplained(violations)}。把内容先交给 Redactor.redact 或 encode_redacted，'
        '调用写在同一个表达式里，守卫才看得出它过了脱敏；RedactedText 经 write_redacted 写。'
        '确实不该脱敏的（Agent 自己的工具、发给模型提供方的请求）在 _ALLOWED 里逐条写明理由。'
    )


def test_every_allowance_is_still_used():
    """没用上的白名单条目，是下一次新增写入现成的掩护。"""
    violations, _ = _scan()

    assert [path for path in _ALLOWED if path not in violations] == []


@pytest.mark.parametrize('anchor', _ANCHORS)
def test_the_scan_really_covered_every_root(anchor: str):
    _, scanned = _scan()

    assert anchor in scanned


def test_the_scan_covers_the_whole_package():
    _, scanned = _scan()

    assert set(scanned) == _package_python_files()
    assert {path.split('/')[0] for path in scanned} >= {'backends', 'cli', 'kernel', 'runner'}


@pytest.mark.parametrize(
    ('relative', 'source', 'expected'),
    [
        ('cli/planted.py', 'def f(p, s):\n    p.write_text(s)\n', ['write_text']),
        (
            'runner/planted.py',
            'def f(client, body):\n    client.post("https://x", json=body)\n',
            ['post json'],
        ),
        (
            'kernel/planted.py',
            'class Mine:\n    def redact(self, x):\n        return x\n',
            ['def redact'],
        ),
        (
            'backends/planted.py',
            'from typing import cast\n'
            'from syndica.kernel.redaction import RedactedText\n'
            'def f(s):\n    return cast(RedactedText, s)\n',
            ['RedactedText outside an annotation'],
        ),
        ('planted.py', 'def f(p):\n    open(p, "w")\n', ['open']),
        (
            'kernel/tools/planted.py',
            'async def f(portal, path, content):\n    await portal.write_text(path, content)\n',
            ['write_text'],
        ),
        (
            'backends/llm_openai/planted.py',
            'async def f(client, url, body):\n    await client.post(url, json=body)\n',
            ['post json'],
        ),
    ],
    ids=[
        'cli',
        'runner',
        'kernel',
        'backends',
        'package-root',
        'new-file-beside-the-file-tools',
        'new-file-beside-the-model-client',
    ],
)
def test_a_violation_planted_in_any_root_is_reported(
    tmp_path: Path, relative: str, source: str, expected: list[str]
):
    planted = tmp_path / relative
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(source, encoding='utf-8')
    (tmp_path / 'clean.py').write_text('X = 1\n', encoding='utf-8')

    violations, scanned = _scan(tmp_path)

    assert _unexplained(violations) == {relative: expected}
    assert sorted(scanned) == sorted([relative, 'clean.py'])


def test_one_more_write_in_a_whitelisted_file_is_reported(tmp_path: Path):
    tools = tmp_path / 'kernel' / 'tools'
    tools.mkdir(parents=True)
    (tools / 'local.py').write_text(
        'async def f(portal, p, c):\n'
        '    await portal.write_text(p, c)\n'
        '    await portal.write_text(p, c)\n',
        encoding='utf-8',
    )

    violations, _ = _scan(tmp_path)

    assert _unexplained(violations) == {'kernel/tools/local.py': ['write_text', 'write_text']}


def test_only_the_redaction_module_may_define_redact_or_build_redacted_text():
    source = (
        'class Redactor:\n'
        '    def redact(self, text):\n'
        '        return text\n'
        'def encode_redacted(value, redactor):\n'
        '    return RedactedText(value)\n'
    )

    assert _violations(source, 'kernel/redaction.py') == []
    assert sorted(_violations(source, 'kernel/other.py')) == [
        'RedactedText outside an annotation',
        'def encode_redacted',
        'def redact',
    ]


@pytest.mark.parametrize(
    ('relative', 'good', 'bad', 'call'),
    [
        (
            'kernel/stores/file_trace.py',
            'from ..redaction import encode_redacted',
            'from .redaction import encode_redacted',
            'encode_redacted(v, r)',
        ),
        (
            'kernel/stores/__init__.py',
            'from ..redaction import encode_redacted',
            'from .redaction import encode_redacted',
            'encode_redacted(v, r)',
        ),
        (
            'kernel/trace.py',
            'from .redaction import encode_redacted',
            'from ..redaction import encode_redacted',
            'encode_redacted(v, r)',
        ),
        (
            'kernel/trace.py',
            'from . import redaction',
            'from .. import redaction',
            'redaction.encode_redacted(v, r)',
        ),
    ],
    ids=['two-levels', 'package-init', 'sibling', 'sibling-module'],
)
def test_a_relative_import_is_resolved_against_the_file_it_is_in(
    relative: str, good: str, bad: str, call: str
):
    assert _violations(f'{good}\np.write_text({call})', relative) == []
    assert _violations(f'{bad}\np.write_text({call})', relative) == ['write_text']


def test_a_violation_planted_in_the_environment_module_is_reported(tmp_path: Path):
    cli = tmp_path / 'cli'
    cli.mkdir()
    (cli / 'environment.py').write_text('def f(p, s):\n    p.write_text(s)\n', encoding='utf-8')
    (cli / 'clean.py').write_text('X = 1\n', encoding='utf-8')

    violations, scanned = _scan(tmp_path)

    assert violations == {'cli/environment.py': ['write_text']}
    assert scanned == ['cli/clean.py', 'cli/environment.py']


def test_a_violation_planted_in_a_nested_subpackage_is_reported(tmp_path: Path):
    nested = tmp_path / 'cli' / 'dev'
    nested.mkdir(parents=True)
    (nested / 'log.py').write_text('def f(p, s):\n    p.write_bytes(s)\n', encoding='utf-8')

    violations, _ = _scan(tmp_path)

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
        ('CONFIG = "x"\nCONFIG = secret\np.write_text(CONFIG)', 'write_text'),
        ('CONFIG = "x"\nCONFIG += secret\np.write_text(CONFIG)', 'write_text'),
        (
            'CONFIG = "x"\ndef f():\n    global CONFIG\n    CONFIG = secret\np.write_text(CONFIG)',
            'write_text',
        ),
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
        (
            'import os\ndef put(self, payload: RedactedText):\n    os.write(fd, payload.encode())',
            'os.write',
        ),
        (
            'from syndica.kernel.redaction import write_all\nwrite_all(fd, text.encode())',
            'write_all',
        ),
        ('from syndica.kernel.redaction import write_all\nwrite_all(fd, data=secret)', 'write_all'),
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
        (
            'import logging.handlers\nlogging.handlers.HTTPHandler(h, u)',
            'logging.handlers.HTTPHandler',
        ),
        (
            'import logging.handlers\nlogging.handlers.SocketHandler(h, n)',
            'logging.handlers.SocketHandler',
        ),
        ('import logging.config\nlogging.config.dictConfig(s)', 'logging.config.dictConfig'),
        ('import socket\nsocket.create_connection(address)', 'socket.create_connection'),
        ('import socket\nsocket.socket()', 'socket.socket'),
        ('import asyncio\nasyncio.open_connection(host, port)', 'asyncio.open_connection'),
        ('import urllib.request\nurllib.request.urlretrieve(u, p)', 'urllib.request.urlretrieve'),
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
        ('argv = ["git", "commit", "-am", message]', '-m'),
        ('argv = ["git", "commit", "-qm", message]', '-m'),
        ('argv = ("git", "commit", f"-m{message}")', '-m'),
        ('argv = ("git", "commit", f"-am{message}")', '-m'),
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
        ('process.communicate(message)', 'communicate input'),
        ('process.communicate(input=message, timeout=5)', 'communicate input'),
        ('process.stdin.write(message.encode())', 'stdin write'),
        ('process.stdin.writelines(lines)', 'stdin write'),
        ('client.post(url, json=body)', 'post json'),
        ('client.put(url, content=text)', 'put content'),
        ('client.patch(url, data=form)', 'patch data'),
        ('client.request("POST", url, json=body)', 'request json'),
        ('client.stream("POST", url, json=body)', 'stream json'),
        ('client.build_request("POST", url, content=text)', 'build_request content'),
        ('transport.send(json=body)', 'send json'),
        ('import httpx\nhttpx.Request("POST", url, json=body)', 'Request json'),
        ('from httpx import post\npost(url, files=upload)', 'post files'),
        ('connection.request("POST", path, body=payload)', 'request body'),
        ('client.post(url, **options)', 'post **kwargs'),
        ('client.post(url, *parts)', 'post *args'),
        ('connection.request("POST", "/v1/traces", payload, headers)', 'request positional'),
        ('import requests\nrequests.post(url, payload)', 'post positional'),
        ('import urllib.request\nurllib.request.Request(url, payload)', 'Request positional'),
        ('from urllib.request import urlopen\nurlopen(url, data=payload)', 'urlopen data'),
        ('import asyncio\nasyncio.to_thread(requests.post, url, json=payload)', 'post json'),
        ('import functools\nfunctools.partial(client.post, url, json=payload)', 'post json'),
        ('retry(client.post, url, content=payload)', 'post content'),
        ('from httpx import post\nretry(post, url, json=payload)', 'post json'),
        ('retry(session.request, "POST", url, payload)', 'request positional'),
        ('sock.sendall(payload)', 'sendall positional'),
        ('ws.send(payload)', 'send positional'),
        ('def redact(text):\n    return text', 'def redact'),
        ('async def encode_redacted(value, redactor):\n    return value', 'def encode_redacted'),
        ('self.redact = lambda text: text', 'assign redact'),
        ('redact = str', 'assign redact'),
        ('first, redact = pair', 'assign redact'),
        ('redact: Callable[[str], str] = str', 'assign redact'),
        ('redact += suffix', 'assign redact'),
        ('(redact := str)', 'assign redact'),
        ('setattr(obj, "redact", str)', 'assign redact'),
        ('object.__setattr__(obj, "redact", str)', 'assign redact'),
        ('types.SimpleNamespace(redact=str)', 'assign redact'),
        ('RedactedText(text)', 'RedactedText outside an annotation'),
        ('redaction.RedactedText(text)', 'RedactedText outside an annotation'),
        ('str.__new__(RedactedText, text)', 'RedactedText outside an annotation'),
        ('cast(RedactedText, text)', 'RedactedText outside an annotation'),
        ('cast(redaction.RedactedText, text)', 'RedactedText outside an annotation'),
        ('typing.cast("RedactedText", text)', 'cast(RedactedText)'),
        ('class Mine(RedactedText):\n    pass', 'RedactedText outside an annotation'),
        ('R = RedactedText', 'RedactedText outside an annotation'),
        ('list(map(RedactedText, texts))', 'RedactedText outside an annotation'),
        ('functools.partial(RedactedText)', 'RedactedText outside an annotation'),
        ('getattr(redaction, "RedactedText")', 'RedactedText outside an annotation'),
        ('RedactedText = str', 'RedactedText outside an annotation'),
        ('from syndica.kernel.redaction import RedactedText as Clean', 'RedactedText alias'),
        ('p.write_text(redact(text))', 'write_text'),
        ('from mylib import redact\np.write_text(redact(text))', 'write_text'),
        ('import mylib\np.write_text(mylib.redact(text))', 'write_text'),
        ('import mylib\nclient.post(url, json=mylib.encode_redacted(v, r))', 'post json'),
        ('import gzip\nclient.post(url, content=gzip.compress(secret))', 'post content'),
        ('def f(payload: str):\n    p.write_text(payload)', 'write_text'),
        ('payload: RedactedText = secret\np.write_text(payload)', 'write_text'),
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
        'p.write_text(self._redactor.redact(text))',
        'from syndica.kernel.redaction import encode_redacted\np.write_text(encode_redacted(v, r))',
        'from syndica.kernel import encode_redacted\np.write_text(encode_redacted(v, r))',
        'from syndica.kernel import redaction\np.write_text(redaction.encode_redacted(v, r))',
        'import syndica.kernel.redaction as red\nclient.post(u, content=red.encode_redacted(v, r))',
        'import gzip\nclient.post(u, content=gzip.compress(redactor.redact(t).encode()))',
        'from syndica.kernel.redaction import write_redacted\nwrite_redacted(fd, t, newline=True)',
        'from syndica.kernel.redaction import write_all\nwrite_all(fd, r.redact(t).encode())',
        'from syndica.kernel.redaction import write_all\nwrite_all(fd, b"*\\n")',
        'from syndica.kernel.redaction import RedactedText',
        'def f(x: RedactedText) -> RedactedText:\n    return x',
        'def f(xs: list[RedactedText]) -> None:\n    pass',
        'x: RedactedText | None = None',
        'isinstance(x, RedactedText)',
        'isinstance(x, (RedactedText, str))',
        'client.post(url, json="{}")',
        'client.get(url, params=query)',
        'runtime.send(agent, message)',
        'asyncio.create_task(runtime.send(agent, message))',
        'def f(request):\n    self._handoff(request, flags)',
        'self._table.put(agent_id, agent)',
        'queue.put(item)',
        'generator.send(None)',
        'client.request(method, url, headers=headers)',
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
        'argv = ["git", "diff", "-M", ref]',
        'argv = ["git", "log", "-n", count]',
        'import os\nos.open(p, os.O_WRONLY | os.O_CREAT)',
        'process.communicate(input=redactor.redact(message).encode("utf-8"), timeout=5)',
        'process.communicate(timeout=5)',
        'process.stdin.write(redactor.redact(message).encode())',
    ],
)
def test_the_guard_allows_content_free_constant_and_redacted_writes(source: str):
    assert _violations(source) == []
