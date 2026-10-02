"""危险判定与处置——**分开的两件事**（spec §5.6）。

**判定是纯规则**：输入是工具与它这次的参数，输出是一个稳定的规则 ID、一个风险等级和一句原因。
它**不读配置、不调 LLM、不决定交互方式**，因此同样的输入必然给出同样的结论，可以被记录、被比对、
被当成事实引用。

**处置由宿主决定**：自动化批处理里危险命令应当直接阻断，交互式产品里应当询问用户。规则里若写死
处置，这套判定就无法跨场景复用——而规则本身是最值得复用的那部分。

**授权作用域只有「这一次调用」。** 不提供「此后均可」。长跑场景下 Agent 会执行数百轮，一次宽泛
授权可能在完全不同的上下文里被用掉。本模块因此不持有任何「已授权」的状态。

**处置的唯一入口是 `disposition_of`，不是 `DangerPolicy.decide`。** spec §12.2 第 3 条要求「高
风险 + 可溯源到不可信内容」时处置**强制升级、不允许模型自授权**；一条能被宿主策略配掉的升级不
满足这个要求，所以升级坐在咨询 policy **之前**。绕过 `disposition_of` 直接调 `decide` 就绕过了
这一条。

**规则表不完备是常态。** 规则只认得出它写过的那些形态；`RiskLevel.NONE` 因此是「没有规则命中」，
不是「已经确认安全」。转手执行的命令当命令再拆一遍：shell `-c` 交出的代码、`eval` 的实参、`xargs`
与 `find -exec` 启动的命令，一层层往下，超过 `_MAX_HANDOFF_DEPTH` 层按命中处理。明确不认的：非 shell
解释器的代码（`python -c "…"`）、名单之外转手执行的程序（`su -c`、`watch`、`ssh`、`make`）、变量与
管道送进 shell 的文本（`sh -c "$CMD"`、`echo … | sh`），以及下载到文件后再单独执行
（`curl -o f; sh f`）。下载来的内容本身被当成代码（`sh -c "$(curl …)"`、`bash <(curl …)`）不在此列：
认的是下载内容的去向。**同一形态换个平凡写法必须仍然命中**——多一段管道、多一次重定向、标志写在
路径后面、路径加一对引号、命令前面加一个绕 alias 的反斜杠（`\\rm`）都不算另一种形态。

`CONFIRM` 由宿主注入的 `ConfirmationHandler` 实现。默认没有 handler 时仍立即拒绝，不白耗等待；
`MailboxConfirmationHandler` 才把请求发进事件流，并经 `Runtime.wait_for` 收回一次性答复。确认使用
独立随机 request id，而不是模型给的 tool call id，迟到答复因此不能批准另一次调用。
"""

from __future__ import annotations

import bisect
import fnmatch
import inspect
import os
import posixpath
import re
import shlex
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast, runtime_checkable

from syndica.kernel.context import ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import Event, EventBus
from syndica.kernel.ids import peek_key
from syndica.kernel.tool import Tool
from syndica.kernel.waiting import Confirmation, WaitResult, WaitState

__all__ = [
    'DEFAULT_RULES',
    'NO_RULE_MATCHED',
    'AllowAll',
    'BlockHighRisk',
    'ConfirmationHandler',
    'ConfirmationOutcome',
    'ConfirmationRequest',
    'DangerJudge',
    'DangerPolicy',
    'DangerRequest',
    'DangerRule',
    'DangerVerdict',
    'Disposition',
    'MailboxConfirmationHandler',
    'RiskLevel',
    'RuleTablePolicy',
    'disposition_of',
    'git_state_change_rule',
    'path_outside_workspace_rule',
    'validate_danger_options',
    'workspace_root_removal_rule',
]


class RiskLevel(Enum):
    """风险等级。

    `NONE` 是「没有规则命中」，不是「已经确认安全」。下游把它读成后者，就是拿一份不完备的规则表
    当安全证明用。

    刻意**不**混入 `str`：混入后 `RiskLevel.HIGH == 'high'` 为真，于是一个漏了 `.value` 的比较、
    或一个拿字符串当等级用的调用方都不会报错，掩盖掉本该现身的错误。
    """

    NONE = 'none'
    LOW = 'low'
    HIGH = 'high'


class Disposition(Enum):
    """对一次调用的处置。`str` 混入的理由同 `RiskLevel`。"""

    ALLOW = 'allow'
    BLOCK = 'block'
    CONFIRM = 'confirm'


class ConfirmationOutcome(Enum):
    """One decision for one confirmation request."""

    APPROVED = 'approved'
    REJECTED = 'rejected'
    TIMEOUT = 'timeout'
    PREEMPTED = 'preempted'
    ABORTED = 'aborted'


@dataclass(frozen=True)
class DangerVerdict:
    """判定结论。字段顺序计入公开 API。

    `rule_id` 是**稳定标识**：它会进日志、进轨迹、被宿主拿去做策略，改一个拼写就是破坏性变更。
    """

    rule_id: str
    level: RiskLevel
    reason: str


NO_RULE_MATCHED = DangerVerdict(
    rule_id='danger.no_rule_matched',
    level=RiskLevel.NONE,
    reason='没有规则命中这次调用的可执行参数；这表示规则表没认出它，不表示已经确认它安全',
)


@dataclass(frozen=True)
class DangerRule:
    """一条规则。`matches` 是纯函数：同样的文本必然同样的结论。

    `matches` 收一整条命令文本而不是解析结果：解析形态是本模块的内部约定，交给第三方规则就成了
    永久兼容承诺。`params` 是构造它的参数（名字、取值）：工作区根的两条规则记下字面的根与解析之后
    的根，回放按它们重建同一条规则（`matches` 是函数，记不下来）。
    """

    rule_id: str
    level: RiskLevel
    reason: str
    matches: Callable[[str], bool]
    params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DangerRequest:
    """交给宿主策略的一次处置请求。字段顺序计入公开 API。

    存在的理由是**可扩展**：`DangerPolicy` 由第三方实现，它的方法签名一经发布就改不动了，而
    spec §12.2 的溯源链要靠产物溯源（§7.3）与 trace 因果链（§11.1）逐步补齐，日后必然要往这里
    加字段。带默认值的新字段对既有实现是无声的；多一个方法参数则让每一个第三方实现当场炸掉。

    `provenance` **没有默认值**：它是「这次调用的参数能不能溯源到不可信内容」的答案，`None` 表示
    **没有溯源过**，与 `TRUSTED`（溯源过，来源都可信）是两件事。给它一个默认值等于让调用方可以
    忘记回答，而忘记的症状是那道强制升级静默消失。

    **不可哈希**：`ToolCall` 可变，于是 frozen 的本类 `hash()` 照样抛 `TypeError`。别拿它当 dict
    键或塞进 set；失败是响亮的，要稳定身份就自己按 `call.id` 取。
    """

    verdict: DangerVerdict
    call: ToolCall
    provenance: TrustLevel | None


@dataclass(frozen=True)
class ConfirmationRequest:
    """An immutable snapshot of one request for human confirmation.

    `request_id` is a fresh nonce and is intentionally distinct from the
    model-provided `tool_call_id`. A late answer can therefore never approve a
    later call that happens to reuse the same model id.

    `request_ref` 是这次请求的稳定键（M3 D10，`wait_for` 随后用同一个键登记它；不在任何工具调用里
    时是 None）：宿主自己写的 handler 按它对上记录下来的答复——回放就是这样给出人的答复的。
    """

    request_id: str
    agent_id: str
    tool_call_id: str
    tool_name: str
    arguments: str
    verdict: DangerVerdict
    request_ref: tuple[str | int, ...] | None = None


class _ConfirmationRuntime(Protocol):
    events: EventBus

    async def wait_for(self, agent_id: str, waiter: Confirmation) -> WaitResult: ...


@runtime_checkable
class ConfirmationHandler(Protocol):
    """Host-selected transport for one confirmation request."""

    async def confirm(
        self,
        runtime: _ConfirmationRuntime,
        request: ConfirmationRequest,
    ) -> ConfirmationOutcome: ...


class MailboxConfirmationHandler:
    """Emit a request event and await one trusted human mailbox reply."""

    def __init__(self, *, timeout: float = 300.0) -> None:
        # Reuse the sealed waiter's complete timeout validation.
        probe = Confirmation(timeout=timeout, tool_call_id='validation-probe')
        self._timeout = probe.timeout

    async def confirm(
        self,
        runtime: _ConfirmationRuntime,
        request: ConfirmationRequest,
    ) -> ConfirmationOutcome:
        payload: dict[str, object] = {
            'request_id': request.request_id,
            'tool_call_id': request.tool_call_id,
            'tool_name': request.tool_name,
            'arguments': request.arguments,
            'verdict': {
                'rule_id': request.verdict.rule_id,
                'level': request.verdict.level.value,
                'reason': request.verdict.reason,
            },
        }
        # 这次请求的稳定键（`wait_for` 随后用同一个键登记它）：回放按键给出人的答复（M3 D10）。
        # 字段名不能像密钥名（`key`、`*_key`）：落盘前的逐叶脱敏会把它下面的 agent id 换掉。
        key = peek_key('request')
        if key is not None:
            payload['request_ref'] = list(key)
        await runtime.events.emit(
            Event(kind='tool_confirmation_requested', agent_id=request.agent_id, payload=payload)
        )
        waited = await runtime.wait_for(
            request.agent_id,
            Confirmation(timeout=self._timeout, tool_call_id=request.request_id),
        )
        if waited.state is WaitState.TIMEOUT:
            return ConfirmationOutcome.TIMEOUT
        if waited.state is WaitState.PREEMPTED:
            return ConfirmationOutcome.PREEMPTED
        if waited.state is WaitState.ABORTED:
            return ConfirmationOutcome.ABORTED
        answer = waited.message
        if answer is None or answer.sender != 'human' or answer.trust is not TrustLevel.TRUSTED:
            return ConfirmationOutcome.REJECTED
        decision = answer.content.strip().lower()
        return (
            ConfirmationOutcome.APPROVED if decision == 'approve' else ConfirmationOutcome.REJECTED
        )


# 命令词之前可以出现的环境变量赋值（`FOO=1 rm -rf x`，bash 还收 `FOO+=1 rm -rf x`）。
_ASSIGNMENT = re.compile(r'\A[A-Za-z_][A-Za-z0-9_]*\+?=')
# 开启一条命令、自己却不是命令的保留字：`{ rm -rf /; }`、`if x; then rm -rf /; fi` 里真正的命令词
# 在它们后面。不当它们是命令词，`{ … }` 与 `then` 就能藏住任何一条命令。
_RESERVED_WORDS = frozenset({'!', '{', 'do', 'elif', 'else', 'if', 'then', 'until', 'while'})
# 这些命令后面跟的仍然是一条命令，因此命令词要继续往后找。
_WRAPPERS = frozenset(
    {
        'builtin',
        'chrt',
        'command',
        'doas',
        'env',
        'exec',
        'flock',
        'ionice',
        'nice',
        'nohup',
        'pkexec',
        'runuser',
        'setsid',
        'stdbuf',
        'su',
        'sudo',
        'taskset',
        'time',
        'timeout',
        'xargs',
        'xvfb-run',
        # `busybox sh`、`busybox wget` 启动的是它后面那个 applet。
        'busybox',
    }
)
# 不把实参当命令执行的程序：有选项表的 wrapper 启动它们时，它们的实参不算候选命令词，见
# `_command_word_slots`。只放确定不执行实参的；拿不准的不放，不放的代价只是一次误报。
_INERT_PROGRAMS = frozenset(
    {'ack', 'ag', 'basename', 'cat', 'chmod', 'cmp', 'cp', 'cut', 'diff', 'dirname', 'du', 'echo'}
    | {'egrep', 'fgrep', 'file', 'grep', 'head', 'ln', 'ls', 'md5sum', 'mkdir', 'mv', 'printf'}
    | {'readlink', 'realpath', 'rg', 'sha256sum', 'shasum', 'sort', 'stat', 'tail', 'touch', 'tr'}
    | {'uniq', 'wc'}
)
_ESCALATORS = frozenset({'doas', 'pkexec', 'runuser', 'su', 'sudo'})
_DOWNLOADERS = frozenset({'aria2c', 'curl', 'fetch', 'lwp-request', 'wget'})
# 解释器的一个选项怎样影响「脚本从哪来」，见 `_script_source`。
_FLAG = 'flag'
# 选项自己交出脚本：取值紧贴时是簇里剩下的部分，否则是下一个词（python 的 `-c` 与 `-m`、perl 的
# `-e`、php 的 `-f`）。stdin 不是脚本。
_SCRIPT = 'script'
# shell 的 `-c` 只是一个模式开关：代码是全部选项之后的第一个操作数，簇里它后面的字母是别的开关
# （`bash -ce "…"` 的 e 是 errexit）。
_CODE_OPERAND = 'code-operand'
# 后面即使还有操作数，脚本仍从 stdin 读：`sh -s x` 的 x 只是 `$1`。
_STDIN = 'stdin'
# 取值是下一个词，除非紧贴在选项后面（`-o pipefail`、`-Wignore`）。
_VALUED = 'valued'
# 取值只能紧贴，簇里它之后的字母都是取值（`perl -MData::Dumper`）。
_ATTACHED = 'attached'
# 紧贴的数字是可选取值，之后簇照常往下读（`perl -le`、`perl -0777e`、`ruby -W2e`）；紧跟 `:` 时
# 簇里剩下的都是取值（`ruby -W:no-deprecated`）。
_OPTIONAL_DIGITS = 'optional-digits'


@dataclass(frozen=True)
class _Interpreter:
    """一个解释器的选项表。短选项可以成簇（`-eux`）；表里没有的选项认不出。"""

    short: Mapping[str, str]
    long: Mapping[str, str]
    # shell 也收 `+o pipefail`、`+e`。
    signs: str = '-'
    # 单独一个 `-`：python、node、perl、ruby 从 stdin 读脚本；shell 只当它是选项结束。
    dash_is_stdin: bool = True
    # 表里没有、名字以这些前缀开头的长选项按开关算（node 的开关数以百计，逐个列不全）。
    flag_prefixes: tuple[str, ...] = ()


def _options(kind: str, names: Iterable[str]) -> dict[str, str]:
    return dict.fromkeys(names, kind)


_SHELL = _Interpreter(
    short={
        **_options(_FLAG, 'abBCdDeEfFhHiklmnpPqrtTuvVxX'),
        'c': _CODE_OPERAND,
        's': _STDIN,
        **_options(_VALUED, 'oO'),
    },
    long={
        **_options(
            _FLAG,
            ('debugger', 'dump-po-strings', 'dump-strings', 'help', 'login', 'noediting')
            + ('noprofile', 'norc', 'posix', 'pretty-print', 'restricted', 'verbose', 'version'),
        ),
        **_options(_VALUED, ('init-file', 'rcfile')),
    },
    signs='-+',
    dash_is_stdin=False,
)
_INTERPRETERS: Mapping[str, _Interpreter] = MappingProxyType(
    {
        **dict.fromkeys(('ash', 'bash', 'csh', 'dash', 'ksh', 'sh', 'tcsh', 'zsh'), _SHELL),
        'fish': _Interpreter(
            short={**_options(_FLAG, 'hilnNPv'), 'c': _SCRIPT, **_options(_VALUED, 'Cdfop')},
            long={
                **_options(
                    _FLAG,
                    ('help', 'interactive', 'login', 'no-config', 'no-execute', 'private')
                    + ('print-debug-categories', 'print-rusage-self', 'version'),
                ),
                'command': _SCRIPT,
                **_options(
                    _VALUED,
                    ('debug', 'debug-output', 'features', 'init-command', 'profile')
                    + ('profile-startup',),
                ),
            },
            dash_is_stdin=False,
        ),
        'node': _Interpreter(
            short={**_options(_FLAG, 'chiv'), **_options(_SCRIPT, 'ep'), **_options(_VALUED, 'Cr')},
            long={
                **_options(
                    _FLAG,
                    ('abort-on-uncaught-exception', 'check', 'enable-source-maps', 'expose-gc')
                    + ('experimental-vm-modules', 'help', 'inspect', 'inspect-brk', 'interactive')
                    + ('no-warnings', 'pending-deprecation', 'preserve-symlinks', 'test')
                    + ('throw-deprecation', 'trace-warnings', 'use-strict', 'version', 'watch'),
                ),
                **_options(_SCRIPT, ('eval', 'print')),
                # 带取值、却与开关前缀同形的列在这里：显式条目先于前缀。
                **_options(
                    _VALUED,
                    ('conditions', 'env-file', 'experimental-loader', 'import', 'input-type')
                    + ('inspect-port', 'loader', 'require', 'title', 'trace-event-categories')
                    + ('trace-event-file-pattern',),
                ),
            },
            flag_prefixes=('experimental-', 'harmony', 'no-', 'trace-'),
        ),
        'perl': _Interpreter(
            short={
                **_options(_FLAG, 'acfhnpsStTuUvwWX'),
                **_options(_SCRIPT, 'eE'),
                # `perl -I lib` 吃掉 lib；`perl -C 7` 里的 7 却是脚本文件，不是 -C 的取值。
                'I': _VALUED,
                **_options(_ATTACHED, 'CdDFimMVx'),
                **_options(_OPTIONAL_DIGITS, '0l'),
            },
            long=_options(_FLAG, ('help', 'version')),
        ),
        'php': _Interpreter(
            short={
                **_options(_FLAG, 'aehHilnqsvw'),
                **_options(_SCRIPT, 'BEfFrRS'),
                **_options(_VALUED, 'cdtz'),
            },
            long={
                **_options(
                    _FLAG,
                    ('help', 'info', 'interactive', 'modules', 'no-php-ini', 'strip')
                    + ('syntax-check', 'version'),
                ),
                **_options(
                    _SCRIPT,
                    ('file', 'process-begin', 'process-code', 'process-end', 'process-file')
                    + ('run', 'server'),
                ),
                **_options(_VALUED, ('define', 'docroot', 'php-ini', 'zend-extension')),
            },
        ),
        **dict.fromkeys(
            ('python', 'python2', 'python3'),
            _Interpreter(
                short={
                    **_options(_FLAG, 'bBdEhiIOPqRsStuvVx3'),
                    **_options(_SCRIPT, 'cm'),
                    **_options(_VALUED, 'WX'),
                },
                long={
                    **_options(_FLAG, ('help', 'help-all', 'help-env', 'help-xoptions', 'version')),
                    'check-hash-based-pycs': _VALUED,
                },
            ),
        ),
        'ruby': _Interpreter(
            short={
                **_options(_FLAG, 'acdhlnpsSvwy'),
                'e': _SCRIPT,
                **_options(_VALUED, 'CEIr'),
                **_options(_ATTACHED, 'FKx'),
                **_options(_OPTIONAL_DIGITS, '0TW'),
            },
            long={
                **_options(
                    _FLAG,
                    ('copyright', 'disable-gems', 'help', 'jit', 'verbose', 'version', 'yjit'),
                ),
                **_options(
                    _VALUED,
                    ('backtrace-limit', 'disable', 'dump', 'enable', 'encoding')
                    + ('external-encoding', 'internal-encoding'),
                ),
            },
        ),
    }
)
# `-c` 交出的是 shell 代码的解释器，`_handoffs` 把那段代码再拆一遍。别的解释器的代码不是 shell
# 语法，当 shell 拆只会认出一堆不存在的命令。
_SHELLS = (_SHELL, _INTERPRETERS['fish'])
_FIND_ACTIONS = frozenset({'-exec', '-execdir', '-ok', '-okdir'})
# 带版本号或构建后缀的名字与不带的是同一个解释器（`python3.11`、`python3.13t`、`python3.12-dbg`、
# `perl5.34`、`php8.2`、`ruby3.2`）。构建后缀只有 python 有：`phpt` 不是 php。
_VERSIONED_INTERPRETER = re.compile(
    r'(python[23]?)[0-9.]*(?:t|-dbg)?|(pypy3?)[0-9.]*|(perl|php|ruby)[0-9][0-9.]*'
)
_INTERPRETER_ALIASES: Mapping[str, str] = MappingProxyType(
    {'nodejs': 'node', 'pypy': 'python3', 'pypy3': 'python3'}
)
# 指向 stdin 的脚本文件：`curl … | bash /dev/stdin` 执行的就是下载来的内容。按规范化之后的路径比，
# `/dev//stdin`、`/dev/./stdin` 是同一个文件。
_STDIN_FILES = frozenset({'/dev/fd/0', '/dev/stdin', '/proc/self/fd/0'})
# `_without_download_substitutions` 换进去的占位词：里面跑了下载器的命令替换（`_DOWNLOADED`）与
# 进程替换（`_DOWNLOADED_FILE`）；接着一个从 stdin 读脚本的解释器的输出进程替换（`_SINK`，
# `>(sh)`）；以及外层判定时，没有下载的嵌套替换所占的位置（`_NESTED`）。私用区字符不会出现在
# 正常命令里；模型真写了它，至多多判一次。
_DOWNLOADED = '\ue000downloaded\ue000'
_DOWNLOADED_FILE = '\ue000downloaded-file\ue000'
_SINK = '\ue000sink\ue000'
_NESTED = '\ue000nested\ue000'
# `/dev/` 下不是块设备的那些：写它们不毁任何原状。
_PSEUDO_DEVICES = frozenset(
    {
        '/dev/full',
        '/dev/null',
        '/dev/random',
        '/dev/stderr',
        '/dev/stdin',
        '/dev/stdout',
        '/dev/tty',
        '/dev/urandom',
        '/dev/zero',
    }
)
# 引号只在分词时消掉：`of="/dev/sda"` 与 `of=/dev/sda` 是同一条命令，逐字节匹配的规则会漏掉前者。
_QUOTES = frozenset('"\'')
# 开启一条新命令的字符。反引号按命令替换处理（`echo \`rm -rf /\`` 真的会执行 rm），因此它也在内。
_BOUNDARIES = frozenset('|;&()\n\r`')
_PIPE = '|'
# **重定向不开启新命令，所以它在分词时先于 `_BOUNDARIES` 被接走。** 这个先后次序是承重的：把
# `<` / `>` 当段边界，`curl x 2>/dev/null | sh` 里的 `/dev/null` 就自成一段、且不由管道接续，
# 于是「这条链里下载过」的状态被清零——任意一次重定向都能打断整条管道链，症状是漏报。
_REDIRECTS = frozenset('<>')
# 重定向操作符可以由这些字符连写而成（`>>` `2>&1` 的 `>&` `&>` `>|`）。只吃紧贴着的那几个：
# 目标词之后的 `|` 仍然是管道。
_REDIRECT_OPERATOR = frozenset('<>&|')
# 取文本的两道上限，见 `_texts`。
_MAX_TEXT_DEPTH = 8
_MAX_TEXT_CHARS = 256 * 1024


# 续行符是反斜杠加 LF。`\` 加 CRLF 不是：CR 是普通字符，`\<CR>` 只是一个转义过的 CR，后面的 LF
# 照样结束这一行——把它也当续行，`echo \<CR><LF>git push` 就把 git push 读成了 echo 的实参。
_CONTINUATION = '\\\n'
# 紧跟在这些字符之后的 `#` 在词首、起注释。`)` 与反引号不在内：`$(date)#tag` 与 `` `date`#tag ``
# 的 `#` 在词中间，当成注释会把同一行后面的命令整个藏起来。CR 也不在内，理由同上。
_WORD_BREAK = frozenset(' \t\n;|&(')


def _escaped(text: str, index: int) -> bool:
    """text[index] 前面紧挨着奇数个反斜杠。"""
    run = index - 1
    while run >= 0 and text[run] == '\\':
        run -= 1
    return (index - 1 - run) % 2 == 1


def _comment_end(text: str, index: int, *, in_backticks: bool) -> int:
    """index 处的 `#` 起的注释在哪结束：行尾；反引号里的注释到收尾的那个反引号为止——反引号里的
    内容先被整段取出来再解析，`` echo `ls #`; sudo ls `` 里的 sudo 照样执行。"""
    newline = text.find('\n', index)
    end = len(text) if newline == -1 else newline
    if in_backticks:
        tick = text.find('`', index, end)
        while tick != -1 and _escaped(text, tick):
            tick = text.find('`', tick + 1, end)
        if tick != -1:
            return tick
    return end


def _starts_a_word(text: str, index: int) -> bool:
    """text[index] 前面是开头或一个没被转义的 `_WORD_BREAK`。续行符不占位置：`a\\<换行>#b` 的 `#`
    在词中间；`\\ #b` 的空格是转义过的字符，`#` 也在词中间。"""
    while index > 0:
        if not _escaped(text, index - 1):
            return text[index - 1] in _WORD_BREAK
        if text[index - 1] != '\n':
            return False
        index -= 2
    return True


def _basename(token: str) -> str:
    """命令词的身份：基名，去掉前导 `\\`，折叠大小写。

    前导 `\\` 一并去掉：`\\rm` 是绕开 `alias rm='rm -i'` 的标准写法，与 `/bin/rm`、`"rm"` 同族。
    折叠大小写：APFS 默认不区分大小写，macOS 上 `GIT push`、`RM -rf /`、`SUDO ls` 找到的就是
    git、rm、sudo；逐字节比较让每一条按程序名认的规则都能靠换个大小写绕过。Linux 上大写的名字多半
    根本不存在，多判只是误报。
    """
    return token.rsplit('/', 1)[-1].lstrip('\\').casefold()


def _interpreter(token: str) -> _Interpreter | None:
    word = _basename(token)
    versioned = _VERSIONED_INTERPRETER.fullmatch(word)
    if versioned:
        word = next(group for group in versioned.groups() if group)
    return _INTERPRETERS.get(_INTERPRETER_ALIASES.get(word, word))


def _is_stdin_file(word: str) -> bool:
    # normpath 保留开头的 `//`（POSIX 允许它另有含义），所以先把开头的斜杠压成一个。
    normalized = posixpath.normpath('/' + word.lstrip('/')) if word.startswith('/') else word
    return normalized in _STDIN_FILES


def _first_operand(tokens: Sequence[str], start: int) -> str | None:
    """start 起第一个不是 `--` 的词：`source` 与 `.` 读的脚本文件。"""
    for index in range(start, len(tokens)):
        if tokens[index] != '--':
            return tokens[index]
    return None


def _runs_a_script(tokens: Sequence[str]) -> bool:
    """这一段启动解释器、`source`、`.` 或 `eval`：同一行的 heredoc 正文可能紧接着就被它执行
    （`cat > f <<'EOF'; sh f`、`eval "$(cat <<'EOF' …)"`）。"""
    return any(
        _interpreter(tokens[index]) is not None
        or _basename(tokens[index]) in ('.', 'source', 'eval')
        for index in _program_candidates(tokens)
    )


def _runs_written_file(
    segments: Sequence[tuple[bool, tuple[str, ...]]], targets: Sequence[str]
) -> bool:
    """同一行里有一段的程序词正是这一行重定向写出的文件（`cat > fix.sh <<'EOF' && ./fix.sh`）。"""
    written = {posixpath.normpath(target) for target in targets}
    return bool(written) and any(
        posixpath.normpath(tokens[index]) in written
        for _, tokens in segments
        for index in _determinate_programs(tokens)
    )


def _end_of_redirection(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index] in _REDIRECT_OPERATOR:
        index += 1
    return index


# 重定向操作符之后那个词的去向，见 `_redirection`。
_TARGET = 'target'
_HEREDOC = 'heredoc'
_HEREDOC_TABS = 'heredoc-tabs'
_DISCARD = 'discard'


def _redirection(source: str, index: int, *, thorough: bool) -> tuple[str, int]:
    """(操作符后面那个词的去向, 操作符结束的位置)。

    heredoc 两种模式都认：正文要单独处理，否则正文里一个撇号（`it's`）会被当成开引号，把结束标记
    之后真正的命令一起吞掉。here-string 只在 `thorough` 下丢掉；默认规则要把它当目标，
    `bash <<< "$(curl …)"` 的下载才看得见。
    """
    if thorough and source.startswith('<<<', index):
        return _DISCARD, index + 3
    if source.startswith('<<<', index):
        return _TARGET, _end_of_redirection(source, index)
    if source.startswith('<<-', index):
        return _HEREDOC_TABS, index + 3
    if source.startswith('<<', index):
        return _HEREDOC, index + 2
    return _TARGET, _end_of_redirection(source, index)


# 把 stdin 读成数据、从不当 shell 执行的程序：`cat > f <<EOF` 写文件、`grep x <<EOF` 过滤、
# `git commit -F - <<EOF` 交提交说明、`read -r -d '' X <<EOF` 赋值、`: <<EOF` 块注释，正文都是数据。
# 两个方向都承重：错放进来一个执行 stdin 的程序，它的正文就不再被看；漏掉一个，写数据的正文被当成
# 命令判，而 git 的提交说明里出现 `rm -rf` 会让默认规则先命中、把本该问人的提交直接拦下。
_STDIN_DATA = frozenset(
    {
        ':',
        'awk',
        'base64',
        'cat',
        'column',
        'curl',
        'cut',
        'diff',
        'egrep',
        'envsubst',
        'fgrep',
        'fold',
        'gawk',
        'gh',
        'git',
        'grep',
        'head',
        'hexdump',
        'jq',
        'less',
        'mapfile',
        'more',
        'nl',
        'od',
        'patch',
        'pbcopy',
        'read',
        'readarray',
        'rev',
        'sed',
        'sort',
        'tac',
        'tail',
        'tee',
        'tr',
        'uniq',
        'wc',
        'xxd',
        'yq',
    }
)


@dataclass
class _Heredoc:
    """一个等着读正文的 heredoc。`owner` 是它所在那一段在段列表里的下标，那一段结束时才填上；
    那一段一个词都没有（`( ssh host ) <<EOF` 喂的是前面的子 shell）时是 None。"""

    delimiter: str
    quoted: bool
    strip_tabs: bool
    owner: int | None = None


def _reads_stdin_as_data(tokens: Sequence[str]) -> bool:
    """没有程序词的一段（`X=1 <<EOF`）把正文读完即丢，也算数据。"""
    return all(_basename(tokens[index]) in _STDIN_DATA for index in _determinate_programs(tokens))


def _feeds_code(
    segments: Sequence[tuple[bool, tuple[str, ...]]], owner: int | None, *, piped_out: bool
) -> bool:
    """owner 那一段读进来的 heredoc 正文会被当成代码执行：它自己不是只把 stdin 当数据的程序；这一行
    它之后有一段经管道接进这样的程序（`cat <<'EOF' | sh`，以及正文经分组流进管道的
    `{ cat <<'EOF'; } | ssh host`）；或者这一行以管道结尾（`cat <<'EOF' |`），看不到谁在读。

    认不出的读者按执行算：`ssh host <<'EOF'` 与 `docker exec -i app sh <<'EOF'` 都执行正文，
    正文写进文件（`ssh host <<'EOF' > log`）说明不了它有没有被执行；没有 owner 时读者是前面的
    子 shell，同样认不出。"""
    if owner is None or piped_out:
        return True
    if not _reads_stdin_as_data(segments[owner][1]):
        return True
    return any(
        piped and not _reads_stdin_as_data(tokens) for piped, tokens in segments[owner + 1 :]
    )


class _Lines:
    """一份文本逐行的索引：从某一行起找第一行等于某个结束标记的。按内容分桶、二分查找，找不到也只
    花对数时间——逐行扫到末尾的话，一串找不到结束标记的 `<<`（`$((1<<0))`、C++ 的 `cout <<`）是
    平方时间，而判定同步跑在事件循环上。被执行的正文是外层文本的一段，嵌套的各层共用最外层这一份
    索引（位置都是最外层文本里的），每层各建一份是深度乘以长度。"""

    def __init__(self, source: str) -> None:
        self._starts: list[int] = []
        self._exact: dict[str, list[int]] = {}
        self._untabbed: dict[str, list[int]] = {}
        position = 0
        for number, line in enumerate(source.split('\n')):
            self._starts.append(position)
            content = line.rstrip('\r')
            self._exact.setdefault(content, []).append(number)
            self._untabbed.setdefault(content.lstrip('\t'), []).append(number)
            position += len(line) + 1
        self._end = len(source)

    def find(
        self, delimiter: str, strip_tabs: bool, position: int, limit: int
    ) -> tuple[int, int] | None:
        """[position, limit) 里第一行等于 delimiter 的：(那一行的起点, 它之后的位置)。position 是某
        一行的起点。"""
        numbers = (self._untabbed if strip_tabs else self._exact).get(delimiter)
        if not numbers:
            return None
        following = bisect.bisect_left(numbers, bisect.bisect_left(self._starts, position))
        if following == len(numbers) or self._starts[numbers[following]] >= limit:
            return None
        number = numbers[following]
        after = self._starts[number + 1] if number + 1 < len(self._starts) else self._end
        return self._starts[number], min(after, limit)


def _substitution_end(text: str, start: int) -> int:
    """`$(` 的内容从 start 起，交回收尾的那个 `)` 的位置；不闭合时交回末尾。"""
    depth = 1
    quote = ''
    index = start
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = '' if char == "'" else quote
        elif char == '\\':
            index += 2
            continue
        elif quote == '"':
            quote = '' if char == '"' else quote
        elif char in _QUOTES:
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text)


def _arithmetic_end(text: str, start: int) -> int | None:
    """`$((` 的内容从 start 起，交回收尾的 `))` 之后的位置。内容里有 `;`、换行、引号或反引号时不是
    算术（`$((cd x; rm -rf y))` 是命令替换里套子 shell），交回 None。"""
    depth = 2
    for index in range(start, len(text)):
        char = text[index]
        if char in ';\n\'"`':
            return None
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _heredoc_expansions(body: str) -> str:
    """结束标记不带引号、不被执行的正文里真正执行的部分：`$(…)` 与反引号里的命令，各占一行。

    正文的其余部分是数据：引号是字符、`#` 不起注释，反斜杠只转义 `$`、`` ` ``、`\\` 与换行。整份
    按命令解析会把 Makefile 正文里的 `rm -rf build` 判成命令、把 `` \\`rm -rf build\\` `` 这种写出
    字面反引号的写法判成执行，而正文里一个撇号又会把后面反引号里真正执行的命令吞进引号。
    """
    commands: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == '\\':
            index += 2
        elif body.startswith('$((', index) and (end := _arithmetic_end(body, index + 3)):
            index = end
        elif body.startswith('$(', index):
            end = _substitution_end(body, index + 2)
            commands.append(body[index + 2 : end])
            index = end + 1
        elif char == '`':
            end = index + 1
            while end < len(body) and body[end] != '`':
                end += 2 if body[end] == '\\' else 1
            commands.append(body[index + 1 : end].replace('\\`', '`'))
            index = end + 1
        else:
            index += 1
    return '\n'.join(commands)


def _heredoc_bodies(
    source: str,
    start: int,
    heredocs: Sequence[_Heredoc],
    lines: _Lines,
    segments: Sequence[tuple[bool, tuple[str, ...]]],
    line_start: int,
    line_targets: Sequence[str],
    *,
    piped_out: bool,
    offset: int,
    spans: list[tuple[int, int, bool]] | None,
) -> tuple[int, list[tuple[str, int | None]]]:
    """(这一行登记的 heredoc 正文结束之后的位置, 要当命令单独解析的文本与它在最外层文本里的起点)；
    每份正文的 (起, 止, 是否被执行) 按最外层文本的位置追加进 `spans`。

    正文可能被执行时整份解析：读它的那条管道会执行它（`_feeds_code`），或者同一行有解释器、
    `eval`（`_runs_a_script`）、跑的正是这一行写出的文件（`_runs_written_file`）。不被执行的正文是
    数据——写进文件（`cat > f <<'EOF'`）、显示或过滤（`grep x <<'EOF'`、
    `git commit -m "$(cat <<'EOF' …)"`）——否则写 README、造模板里出现 `rm -rf` 就成了误报；其中
    结束标记不带引号的只解析它展开时执行的部分（`_heredoc_expansions`）。写进文件、下一行才执行的
    正文看不见，与 `echo '…' > f` 之后再执行同属模块开头的盲区。

    单独解析是为了正文里的引号吞不到结束标记之后。结束标记找不齐就一份都不单独处理：跳到末尾会
    把后面真正的命令一起藏起来。
    """
    position = start
    located: list[tuple[_Heredoc, int, int]] = []
    for heredoc in heredocs:
        found = lines.find(
            heredoc.delimiter, heredoc.strip_tabs, offset + position, offset + len(source)
        )
        if found is None:
            return start, []
        located.append((heredoc, position, found[0] - offset))
        position = found[1] - offset
    line = segments[line_start:]
    runs_a_script = any(_runs_a_script(tokens) for _, tokens in line)
    runs_a_script = runs_a_script or _runs_written_file(line, line_targets)
    parsed: list[tuple[str, int | None]] = []
    for heredoc, body_start, body_end in located:
        body = source[body_start:body_end]
        executed = runs_a_script or _feeds_code(segments, heredoc.owner, piped_out=piped_out)
        if executed:
            parsed.append((body, offset + body_start))
        elif not heredoc.quoted:
            parsed.append((_heredoc_expansions(body), None))
        if spans is not None:
            spans.append((offset + body_start, offset + body_end, executed))
    return position, parsed


# `$'…'` 里的反斜杠转义：`$'\x67it' push` 就是 `git push`。
_ANSI_C = "$'"
_ANSI_C_SIMPLE: Mapping[str, str] = MappingProxyType(
    {
        'a': '\a',
        'b': '\b',
        'e': '\x1b',
        'E': '\x1b',
        'f': '\f',
        'n': '\n',
        'r': '\r',
        't': '\t',
        'v': '\v',
        '\\': '\\',
        "'": "'",
        '"': '"',
        '?': '?',
    }
)
_ANSI_C_NUMERIC = (('x', 16, 2), ('u', 16, 4), ('U', 16, 8))


def _ansi_c_escape(source: str, index: int) -> tuple[str, int]:
    """source[index] 是 `$'…'` 里的反斜杠：(解码出的文本, 转义之后的位置)。认不出的原样留下。"""
    following = source[index + 1 : index + 2]
    if not following:
        return '\\', index + 1
    if following in _ANSI_C_SIMPLE:
        return _ANSI_C_SIMPLE[following], index + 2
    # bash 先按「反斜杠加一个字符」找出 `$'…'` 在哪结束、再解码：`\c` 后面的 `'` 仍是收尾的引号。
    if following == 'c' and source[index + 2 : index + 3] not in ('', "'", '\\'):
        return chr(ord(source[index + 2]) & 0x1F), index + 3
    for letter, base, width in _ANSI_C_NUMERIC:
        if following == letter:
            digits = ''
            for char in source[index + 2 : index + 2 + width]:
                if char not in '0123456789abcdefABCDEF':
                    break
                digits += char
            if digits and int(digits, base) <= 0x10FFFF:
                return chr(int(digits, base)), index + 2 + len(digits)
            return '\\' + following, index + 2
    if following in '01234567':
        digits = ''
        for char in source[index + 1 : index + 4]:
            if char not in '01234567':
                break
            digits += char
        return chr(int(digits, 8) & 0xFF), index + 1 + len(digits)
    return '\\' + following, index + 2


# 双引号里反斜杠只转义这几个字符，别的反斜杠照原样留着。
_DOUBLE_QUOTE_ESCAPES = frozenset('"\\$`')
# heredoc 正文里又套 heredoc 时单独解析的层数上限；超过就按原来的方式接着往下分词。
_MAX_HEREDOC_NESTING = 16
# 转手执行的命令（`_handoffs`）一层层再拆的层数上限。再往下还有转手就按命中处理：层数由模型决定，
# 读不到的那一层里可能正是要拦的命令。
_MAX_HANDOFF_DEPTH = 8
# 代替读不到的那一层的段，每条规则都按命中处理。命令里照抄这个词，后果只是多判。
_TOO_DEEP = '\ue000too-deep\ue000'


def _segments(text: str) -> list[tuple[bool, tuple[str, ...]]]:
    """把一条命令拆成若干段，每段是 (是否由管道接在上一段之后, 去掉引号的 token)。

    重定向（`2>/dev/null`、`</dev/null`、`2>&1`、`&>log`）既不开新段也不留 token：fd 前缀与操作符
    属于语法，目标是一个路径。**目标不能留作 token**——留着它，`curl x | sh > /tmp/out` 的第二段就
    多出一个非选项词，「解释器带脚本实参」那条判据会把它读成「执行的是本地脚本」。要看目标的规则
    用 `_parse`。

    引号不成对、反引号不闭合都不报错：这条文本来自模型，判定在它上面抛异常等于让一次畸形输入
    变成整轮失败。拆不干净的部分按原样留在 token 里，规则至多多认几种。
    """
    return _parse(text)[0]


def _parse(
    text: str,
    *,
    thorough: bool = False,
    nesting: int = 0,
    spans: list[tuple[int, int, bool]] | None = None,
    offset: int = 0,
    lines: _Lines | None = None,
    handoffs: bool = True,
) -> tuple[list[tuple[bool, tuple[str, ...]]], list[str]]:
    """(段, 整条命令里全部重定向的目标)。

    `thorough=False` 时段与 `_segments` 一字不差：默认规则的判定只取决于段。三族可选规则用
    `thorough=True`，多做三件会改变段的事，所以它们不进默认规则：
    - 空引号是一个词：`git -C '' push` 的 `''` 是 `-C` 的取值，丢掉它，`push` 就被当成取值吞掉；
    - 双引号里的 `$(…)` 与反引号照样执行，拆成新的一段；
    - here-string 的词既不是 token 也不是目标。

    两种模式都按 shell 的规则消掉反斜杠与 `$'…'`、跳过注释：引号状态一旦算错，后面整条命令就被
    当成引号里的字符、对所有规则隐身（`echo it\\'s; rm -rf /`、`# Let's push` 之后的 `git push`）。
    续行符只在 shell 真的续行的地方消掉——引号外与双引号里；单引号、注释、heredoc 正文里的 `\\` 加
    换行照原样留着，否则正文最后一行的 `\\` 会把结束标记粘进正文。heredoc 两种模式都认，见
    `_heredoc_bodies`。

    目标不挂在段上：只有重定向、没有命令词的一段（`: ; > /etc/x` 的后半）不产出段，挂在段上它的
    目标就丢了。`2>&1` 的目标是 `1`——相对的词，按路径看落在根里。

    `spans` 收下每一层 heredoc 正文的 (起, 止, 是否被执行)，位置是最外层文本里的；`offset` 是这份
    text 在最外层文本里的起点、`lines` 是最外层文本的逐行索引，递归解析被执行的正文时传下去。

    `handoffs` 时转手执行的命令也拆成段、接在后面，见 `_with_handoffs`。递归解析正文时不接：
    正文的段并进了这一层，由最外层一次接完，否则同一条命令要拆两遍。
    """
    segments: list[tuple[bool, tuple[str, ...]]] = []
    targets: list[str] = []
    tokens: list[str] = []
    token: list[str] = []
    heredocs: list[_Heredoc] = []
    unowned: list[_Heredoc] = []
    closers: list[str] = []
    piped = False
    quote = ''
    quoted = False
    dropping = ''
    line_start = 0
    target_line_start = 0
    cut = False
    in_backticks = False
    source = text
    index = 0

    def end_token() -> None:
        nonlocal dropping, quoted
        if token or (thorough and quoted):
            word = ''.join(token)
            if dropping == _TARGET:
                targets.append(word)
            elif dropping in (_HEREDOC, _HEREDOC_TABS):
                heredoc = _Heredoc(
                    word.replace('\\', ''), quoted or '\\' in word, dropping == _HEREDOC_TABS
                )
                heredocs.append(heredoc)
                unowned.append(heredoc)
            elif not dropping:
                tokens.append(word)
            dropping = ''
            token.clear()
        quoted = False

    def end_segment(next_piped: bool) -> None:
        nonlocal piped, dropping
        end_token()
        dropping = ''
        for heredoc in unowned:
            heredoc.owner = len(segments) if tokens else None
        unowned.clear()
        if tokens:
            segments.append((piped, tuple(tokens)))
            tokens.clear()
            piped = next_piped
            return
        # 没吐出段就不要覆盖 piped：`|&` 的 `&` 与 `||` 的第二个 `|` 都落在一个空段上，赋值会让
        # 它们把刚立起来的管道标记又抹掉。
        piped = piped or next_piped

    while index < len(source):
        char = source[index]
        if quote == _ANSI_C:
            if char == '\\':
                decoded, index = _ansi_c_escape(source, index)
                # bash 在 NUL 处截断这个词：`$'rm\0' -rf x` 跑的就是 rm。
                cut = cut or '\x00' in decoded
                if not cut:
                    token.append(decoded)
                continue
            if char == "'":
                quote = ''
                cut = False
            elif not cut:
                token.append(char)
        elif quote:
            if quote == '"' and char == '\\':
                following = source[index + 1 : index + 2]
                if source.startswith(_CONTINUATION, index):
                    index += 2
                    continue
                if following in _DOUBLE_QUOTE_ESCAPES:
                    token.append(following)
                    index += 2
                    continue
            if char == quote:
                quote = ''
            elif thorough and quote == '"' and (char == '`' or source.startswith('$(', index)):
                closers.append('`' if char == '`' else ')')
                quote = ''
                end_segment(False)
                index += 1 if char == '`' else 2
                continue
            else:
                token.append(char)
        elif char == '\\':
            # 引号外的反斜杠让下一个字符只是字符：`\'` 不开引号、`\;` 不分段、`r\m` 就是 rm。反斜杠
            # 加换行是续行：`rm \<换行>-rf x` 是一条命令。反引号里的 `` \` `` 例外：它是嵌套的命令
            # 替换，当它是边界，里面的命令才看得见；反引号外它只是一个字面的反引号。
            if source.startswith(_CONTINUATION, index):
                index += 2
                continue
            following = source[index + 1 : index + 2]
            if following == '`' and (in_backticks or '`' in closers):
                end_segment(False)
                index += 2
                continue
            if following:
                token.append(following)
                quoted = True
                index += 2
                continue
            token.append(char)
        elif char == '#' and _starts_a_word(source, index):
            # 词首的 `#` 起注释：注释里的撇号（`# Let's push`）不是开引号。
            index = _comment_end(source, index, in_backticks=in_backticks or '`' in closers)
            continue
        elif closers and char == ')' and closers[-1] == '(':
            closers.pop()
            end_segment(False)
        elif closers and char == closers[-1] != '(':
            closers.pop()
            end_segment(False)
            quote = '"'
        elif source.startswith('$$', index):
            # `$$` 是进程号；后面跟的 `'…'` 是普通单引号，不是 `$'…'`。
            token.append('$$')
            index += 2
            continue
        elif source.startswith('$((', index) and (end := _arithmetic_end(source, index + 3)):
            # 算术展开是一个词，里面的 `<<` 是位移，不是 heredoc。
            token.append(source[index:end])
            index = end
            continue
        elif char == '$' and source[index + 1 : index + 2] == "'":
            quote = _ANSI_C
            quoted = True
            index += 2
            continue
        elif char in _QUOTES:
            quote = char
            quoted = True
        elif char in _REDIRECTS or (char == '&' and source[index + 1 : index + 2] == '>'):
            if char in _REDIRECTS and ''.join(token).isdigit():
                # `2>` 的 `2` 是 fd 编号、是操作符的一部分；`hi2>x` 的 `hi2` 才是一个词。
                token.clear()
            end_token()
            dropping, index = _redirection(source, index, thorough=thorough)
            continue
        elif char in _BOUNDARIES:
            if closers and char == '(':
                # 双引号里 `$(…)` 中的子 shell 与 `$((…))`：它们的 `)` 不结束这个替换，
                # `"$( (true); git push )"` 里的 git push 才不会被当成引号里的字符。
                closers.append('(')
            if char == '`':
                in_backticks = not in_backticks
            end_segment(char == _PIPE)
            if char == '\n':
                if heredocs and nesting < _MAX_HEREDOC_NESTING:
                    lines = _Lines(source) if lines is None else lines
                    index, bodies = _heredoc_bodies(
                        source,
                        index + 1,
                        heredocs,
                        lines,
                        segments,
                        line_start,
                        targets[target_line_start:],
                        piped_out=piped,
                        offset=offset,
                        spans=spans,
                    )
                    for body, body_offset in bodies:
                        # 只展开不执行的正文取出的是一份新文本：位置对不上外层，另起索引。
                        body_segments, body_targets = _parse(
                            body,
                            thorough=thorough,
                            nesting=nesting + 1,
                            spans=None if body_offset is None else spans,
                            offset=0 if body_offset is None else body_offset,
                            lines=None if body_offset is None else lines,
                            handoffs=False,
                        )
                        segments.extend(body_segments)
                        targets.extend(body_targets)
                    heredocs.clear()
                    line_start = len(segments)
                    target_line_start = len(targets)
                    continue
                heredocs.clear()
                line_start = len(segments)
                target_line_start = len(targets)
        elif char.isspace():
            end_token()
        else:
            token.append(char)
        index += 1
    end_segment(False)
    if handoffs:
        _with_handoffs(segments, targets, thorough=thorough)
    return segments, targets


def _with_handoffs(
    segments: list[tuple[bool, tuple[str, ...]]], targets: list[str], *, thorough: bool
) -> None:
    """把每段转手执行的命令拆出来的段与重定向目标接在全部段之后，一层层往下。第
    `_MAX_HANDOFF_DEPTH` 层的段还在转手时接一段 `_TOO_DEEP`。同一份文本只拆一次。

    接上的段的 `piped` 只在它自己那份文本里有意义，所以读管道链的 `shell.remote_script_execution`
    不用这里，自己逐层扫（`_runs_downloaded_content`）。
    """
    level: list[tuple[str, ...]] = [tokens for _, tokens in segments]
    seen: set[str] = set()
    for depth in range(_MAX_HANDOFF_DEPTH + 1):
        texts = [text for tokens in level for text in _handoffs(tokens) if text not in seen]
        if not texts:
            return
        if depth == _MAX_HANDOFF_DEPTH:
            segments.append((False, (_TOO_DEEP,)))
            return
        level = []
        for text in dict.fromkeys(texts):
            seen.add(text)
            inner, inner_targets = _parse(text, thorough=thorough, handoffs=False)
            segments.extend(inner)
            targets.extend(inner_targets)
            level.extend(tokens for _, tokens in inner)


def _handoffs(tokens: Sequence[str]) -> list[str]:
    """这一段转手执行的命令，每条是一份要再拆一遍的文本：shell `-c` 交出的代码、`eval` 拼起来的
    实参，或 `find` 每个动作执行的命令（argv 原样转回文本，见 `_find_actions`）。

    只认一段里第一个转手的程序：它之后的词是它的实参。`sudo eval eval …` 里每个 `eval` 都是候选
    命令词，每个都取一遍「它之后的全部」就是平方，而判定跑在每一次工具调用上。不交代码的 shell
    之后从它的第一个操作数接着找：它可能只是认不出的 wrapper 选项的取值（`sudo -u bash sh -c …`），
    但它的选项与取值不再当候选——`env sh -o sh -o …` 里每个 `sh` 都从头扫一遍选项，也是平方。
    shell 的选项认不清时（见下面那个分支）把余下每个操作数都交出去：漏掉 `-c` 的代码，七条规则就
    一起看不见它。
    """
    resume = 0
    for index in _program_candidates(tokens):
        if index < resume:
            continue
        name = _basename(tokens[index])
        if name == 'eval':
            return [' '.join(tokens[index + 1 :])] if index + 1 < len(tokens) else []
        if name == 'find':
            return [shlex.join(command) for _, command in _find_actions(tokens[index + 1 :])]
        interpreter = _interpreter(tokens[index])
        if interpreter in _SHELLS:
            kind, code, resume = _script_scan(interpreter, tokens, index + 1)
            if kind == 'option':
                return [code]
            if kind == 'unknown' or any(_names_dash_c(word) for word in tokens[index + 1 : resume]):
                # 认不出的选项可能正是吃掉 `-c` 的那个，`-c` 也可能被当成了别的选项的取值（zsh 的
                # `-O` 是开关，选项表按 bash 让它吃一个词）：认不清就把余下每个操作数都当代码。
                return [word for word in tokens[resume:] if not word.startswith(('-', '+'))]
    return []


def _names_dash_c(word: str) -> bool:
    """一个短选项（或短选项簇）里有 `c`。"""
    return len(word) > 1 and word[0] in '-+' and word[1] != '-' and 'c' in word[1:]


def _find_actions(arguments: Sequence[str]) -> list[tuple[int, tuple[str, ...]]]:
    """`find` 的每个 `-exec` / `-execdir` / `-ok` / `-okdir`：(动作的下标, 它执行的命令)。命令到
    终止符为止（`_ends_find_command`），没有终止符就到段尾——不带反斜杠的 `;` 在 shell 里先把段
    断开了。"""
    actions: list[tuple[int, tuple[str, ...]]] = []
    index = 0
    while index < len(arguments):
        if arguments[index] in _FIND_ACTIONS:
            end = index + 1
            while end < len(arguments) and not _ends_find_command(arguments, end):
                end += 1
            if end > index + 1:
                actions.append((index, tuple(arguments[index + 1 : end])))
            index = end
        index += 1
    return actions


def _ends_find_command(arguments: Sequence[str], index: int) -> bool:
    """`-exec` 的命令在这里结束：`+` 只在紧跟 `{}` 时才是终止符（别处是普通实参），以 `;` 开头的词
    都是（BSD 的 find 就这么认，`';x'` 也结束命令）。"""
    word = arguments[index]
    return word.startswith(';') or (word == '+' and index > 0 and arguments[index - 1] == '{}')


def _too_deep(segments: Sequence[tuple[bool, tuple[str, ...]]]) -> bool:
    return any(tokens == (_TOO_DEEP,) for _, tokens in segments)


def _command_word_slots(tokens: Sequence[str], start: int = 0) -> list[tuple[int, str]]:
    """一段里处在**命令词位置**上的 (下标, 基名)。

    按位置取而不是「这段里出现过这个词」：后者让 `echo sudo apt install` 与 `git push --force`
    这类散文式参数每次都命中，而误报会被下游当噪声整条关掉。

    **wrapper 之后不再停在第一个非选项词上。** 哪些 wrapper 选项要吃掉下一个词无法从语法判断，
    而 `nice -n 19 rm` 的 `19` 是 `-n` 的取值、`timeout 30 rm` 的 `30` 是位置实参：停在它上面就
    把真正的命令词整个吞掉，`nice -n 19 rm -rf /` 于是在默认规则表下一条闸都没有。代价是 wrapper
    之后的普通实参也算候选命令词（`command -v sudo` 会误报成提权），这个方向的错还能换个写法，
    反方向的错是一句不说地放行。第一个命令词之前的保留字（`{`、`then`、`!`）跳过。

    例外只有一种：有选项表的 wrapper（`_WRAPPER_GRAMMARS`）选项全认得出，启动的又是不执行实参的程序
    （`_INERT_PROGRAMS`）。这时它的实参不算候选——`xargs grep -n git src/` 里的 `git` 是 grep 的
    模式，当成程序词，开发期的 git 闸就会无条件拦下一次普通的搜索。启动的是名单之外的程序时照旧，
    之后每个词都算候选：`nice strace rm -rf /` 的 `strace` 自己就会执行后面的命令，而这类 runner 列
    不全，不执行实参的那一边才列得全。启动的是 wrapper 时接着往下认。
    """
    slots: list[tuple[int, str]] = []
    wrapped = False
    index = start
    while index < len(tokens):
        token = tokens[index]
        if (
            token.startswith('-')
            or _ASSIGNMENT.match(token)
            or (not slots and token in _RESERVED_WORDS)
        ):
            index += 1
            continue
        word = _basename(token)
        slots.append((index, word))
        if not wrapped:
            if word not in _WRAPPERS:
                break
            following = _wrapped_program(tokens, index)
            if following is not None:
                program = _basename(tokens[following])
                if program in _WRAPPERS:
                    index = following
                    continue
                if program in _INERT_PROGRAMS:
                    slots.append((following, program))
                    break
            wrapped = True
        index += 1
    return slots


def _command_words(tokens: Sequence[str]) -> list[str]:
    return [word for _, word in _command_word_slots(tokens)]


def _escalates_privilege(command: str) -> bool:
    """`sudo` / `doas` / `su` / `pkexec` / `runuser` 出现在命令词位置。

    `sudo -v`（刷新凭据）与 `sudo -l`（列权限）也算：它们不执行别的命令，判 HIGH 是刻意的——
    区分「这次 sudo 会不会真做事」要认全每个 sudo 选项，而认漏一个就是一道闸没了。
    """
    segments = _segments(command)
    return _too_deep(segments) or any(
        word in _ESCALATORS for _, tokens in segments for word in _command_words(tokens)
    )


def _next_word(arguments: Sequence[str], index: int) -> str:
    return arguments[index] if index < len(arguments) else ''


def _script_source(interpreter: _Interpreter, arguments: Sequence[str]) -> tuple[str, str]:
    """解释器从哪里取脚本：(种类, 相关的词)。

    种类：`stdin`（从 stdin 读）；`option`（选项或 shell `-c` 交出的代码，词是那段代码 / 模块 /
    文件）；`file`（词是脚本文件）；`unknown`（有认不出的选项——它可能吃掉下一个词，那个词就不一定
    是脚本）。第一个操作数是脚本文件，除非 `-s` 让解释器仍从 stdin 读（操作数只是位置参数），或
    shell 的 `-c` 在等它的代码（`bash -ce CODE` 的 CODE）。
    """
    kind, word, _ = _script_scan(interpreter, arguments, 0)
    return kind, word


def _script_scan(
    interpreter: _Interpreter, tokens: Sequence[str], start: int
) -> tuple[str, str, int]:
    """`_script_source`，从 `tokens[start]` 读起，另交回停在哪个下标：它之前的词都是解释器的选项与
    选项取值。不切片：对一段里每个候选命令词各切一次就是平方。"""
    reading_stdin = False
    code_pending = False
    index = start
    while index < len(tokens):
        word = tokens[index]
        index += 1
        if word == '-' and interpreter.dash_is_stdin and not code_pending:
            return _STDIN, '', index
        if word in ('-', '--'):
            if code_pending:
                if index < len(tokens):
                    return 'option', tokens[index], index
                return _STDIN, '', index
            if reading_stdin or index >= len(tokens):
                return _STDIN, '', index
            return 'file', tokens[index], index
        if not (word[:1] in interpreter.signs and len(word) > 1):
            if code_pending:
                return 'option', word, index - 1
            return (_STDIN, '', index - 1) if reading_stdin else ('file', word, index - 1)
        if word.startswith('--'):
            name, attached, value = word[2:].partition('=')
            kind = interpreter.long.get(name)
            if kind is None:
                # 取值紧贴的长选项吃不掉下一个词；合乎已知开关前缀的（node 的 `--no-*`）也是开关。
                if attached or name.startswith(interpreter.flag_prefixes):
                    continue
                return 'unknown', '', index
            if kind == _SCRIPT:
                return 'option', value if attached else _next_word(tokens, index), index
            if kind == _VALUED and not attached:
                index += 1
            continue
        cluster = word[1:]
        position = 0
        while position < len(cluster):
            kind = interpreter.short.get(cluster[position])
            position += 1
            rest = cluster[position:]
            if kind is None:
                return 'unknown', '', index
            if kind == _SCRIPT:
                return 'option', rest or _next_word(tokens, index), index
            if kind == _CODE_OPERAND:
                code_pending = True
            elif kind == _STDIN:
                reading_stdin = True
            elif kind == _VALUED:
                if not rest:
                    index += 1
                break
            elif kind == _ATTACHED:
                break
            elif kind == _OPTIONAL_DIGITS:
                while position < len(cluster) and cluster[position].isdigit():
                    position += 1
                if position < len(cluster) and cluster[position] == ':':
                    break
    return _STDIN, '', index


def _reads_its_script_from_stdin(tokens: Sequence[str]) -> bool:
    """这一段里有没有一个把 stdin 当脚本执行的程序。

    带脚本实参的解释器执行的是那个文件，管道送进来的只是它的 stdin——`curl … | python3 x.py` 是
    每天都在跑的数据管道，判它远程脚本执行就是误报。`sh -s x`、`python3 - x` 读的仍是 stdin，
    `bash -o pipefail` 的 `pipefail` 也不是脚本文件；`bash /dev/stdin`、`source /dev/stdin` 读的同样
    是 stdin（路径按规范化之后比）。解释器按候选程序词找，`uv run python` 同样算。
    """
    for index in _program_candidates(tokens):
        if _basename(tokens[index]) in ('.', 'source'):
            operand = _first_operand(tokens, index + 1)
            if operand is not None and _is_stdin_file(operand):
                return True
            continue
        interpreter = _interpreter(tokens[index])
        if interpreter is None:
            continue
        kind, word = _script_source(interpreter, tokens[index + 1 :])
        # 选项交出的脚本文件同样可能就是 stdin（`php -f /dev/stdin`）。
        if kind in (_STDIN, 'unknown') or (kind in ('file', 'option') and _is_stdin_file(word)):
            return True
    return False


def _carries_download(word: str) -> bool:
    return _DOWNLOADED in word or _DOWNLOADED_FILE in word


def _downloads(tokens: Sequence[str]) -> bool:
    """这一段里下载器出现在程序词位置。与解释器按同一种候选找：`uv run curl`、`busybox wget`
    也算。"""
    return any(_basename(tokens[index]) in _DOWNLOADERS for index in _program_candidates(tokens))


@dataclass
class _Layer:
    """扫描中还没闭合的一层替换。最外层的 `opener` 是空串。

    `text` 是替换之后的文本；`probe` 是判定这一层用的文本——没有下载的嵌套替换在这里只占一个
    `_NESTED`，于是每个字符只被判定一次：否则每一层都把里面整段重新分词，深度为 d 的嵌套就是
    d 倍的输入长度。
    """

    opener: str
    text: list[str] = field(default_factory=list[str])
    probe: list[str] = field(default_factory=list[str])
    depth: int = 0
    in_double: bool = False

    def add(self, piece: str) -> None:
        self.text.append(piece)
        self.probe.append(piece)


_PROCESS_OPENERS = ('<(', '>(')


def _close_layer(layers: list[_Layer], *, closed: bool, handoffs: list[str]) -> bool:
    """弹出最里面那一层，交给外层：占位词，或原样放回。

    交回这一层自己的命令是不是已经在执行下载来的内容（`: $(curl … | sh)`）：换成占位词之后，外层
    只看得见「这里有下载」，看不见它里面那条管道。这一层转手执行的命令收进 `handoffs`：换成占位
    词、或放回双引号里的那一层，外层的分词都不会再把它拆出来。
    """
    layer = layers.pop()
    parent = layers[-1]
    probe = ''.join(layer.probe)
    segments, targets = _parse(probe, handoffs=False)
    handoffs.extend(text for _, tokens in segments for text in _handoffs(tokens))
    hit = _runs_parsed(segments, targets)
    if layer.opener == '>(':
        # 输出进程替换把写进来的内容当 stdin：接的是从 stdin 读脚本的解释器，写进来的就会被执行。
        if any(_reads_its_script_from_stdin(tokens) for _, tokens in segments):
            parent.add(_SINK)
            return hit
    elif _carries_download(probe) or any(_downloads(tokens) for _, tokens in segments):
        parent.add(_DOWNLOADED_FILE if layer.opener == '<(' else _DOWNLOADED)
        return hit
    closer = '`' if layer.opener == '`' else ')'
    parent.text.append(layer.opener + ''.join(layer.text) + (closer if closed else ''))
    parent.probe.append(_NESTED)
    return hit


def _without_download_substitutions(
    text: str,
    handoffs: list[str],
    bodies: dict[int, tuple[int, bool]] | None = None,
    offset: int = 0,
) -> tuple[str, bool]:
    """把里面跑了下载器的命令替换（`$(…)`、反引号）换成 `_DOWNLOADED`、进程替换（`<(…)`）换成
    `_DOWNLOADED_FILE`，把接着从 stdin 读脚本的解释器的输出进程替换（`>(sh)`）换成 `_SINK`；
    嵌套的一层层往外传。另交回某一层自己是否已经在执行下载来的内容，见 `_close_layer`。

    替换之后，下载来的内容在命令里就是一个看得见的词：它是 `sh -c` 的代码、`eval` 的实参、`bash`
    读的脚本文件，还是交给本地程序的普通实参，由各自的判据决定。`$((…))` 是算术不是替换；`<(`、
    `>(` 只在双引号外才是进程替换。单引号里的不换——它在这一层不执行；交给 `eval` 或 `sh -c` 的
    字符串在下一层执行，各层转手执行的命令收进 `handoffs`，由 `_runs_downloaded_content` 当一份新
    文本再扫一遍。

    替换的嵌套不递归：深度由模型决定，递归会在一段畸形输入上撞出 RecursionError，而判定抛异常等于
    让这次调用所在的整批失败。

    heredoc 正文不跟着外面的引号状态扫：正文里的撇号（`it's done`、Python 的 `'don\\'t'`）不是外面
    那条命令的开引号，当成开引号就把结束标记之后的 `sh -c "$(curl …)"` 一起吞进引号。不被执行的
    正文原样放过，被执行的正文单独扫。正文在哪、执不执行由 `_parse` 判——同一个判据，扫描与分词
    才不会各认一套；用 thorough，`"$(cat <<'EOF' …)"` 里的 heredoc 也认得出。各层正文的位置在最外层
    解析一次就全部拿到（`bodies` 与 `offset` 往下传），每层各自重新解析是深度的平方；嵌套深度由
    `_MAX_HEREDOC_NESTING` 封顶。不带引号的正文里 `$(curl … | sh)` 照样执行，由 `_parse` 解析正文
    展开的部分时认出。
    """
    if bodies is None:
        found: list[tuple[int, int, bool]] = []
        _parse(text, thorough=True, spans=found, handoffs=False)
        bodies = {start: (end, executed) for start, end, executed in found}
    layers = [_Layer('')]
    hit = False
    index = 0
    while index < len(text):
        # 取走而不是查看：空正文的起止相同，只查看会在同一个位置上转个不停。
        body = bodies.pop(offset + index, None)
        if body is not None:
            end = body[0] - offset
            content = text[index:end]
            if body[1]:
                content, body_hit = _without_download_substitutions(
                    content, handoffs, bodies, offset + index
                )
                hit = body_hit or hit
            layers[-1].add(content)
            index = end
            continue
        layer = layers[-1]
        char = text[index]
        parenthesised = layer.opener not in ('', '`') and not layer.in_double
        if char == '\\':
            layer.add(text[index : index + 2])
            index += 2
        elif char == "'" and not layer.in_double:
            end = text.find("'", index + 1)
            stop = len(text) if end == -1 else end + 1
            layer.add(text[index:stop])
            index = stop
        elif char == '"':
            layer.in_double = not layer.in_double
            layer.add(char)
            index += 1
        elif (char == '`' and layer.opener == '`') or (
            char == ')' and parenthesised and not layer.depth
        ):
            hit = _close_layer(layers, closed=True, handoffs=handoffs) or hit
            index += 1
        elif char == '#' and not layer.in_double and _starts_a_word(text, index):
            # 词首的 `#` 起注释：注释里的撇号不是开引号，注释里的 `$(…)` 也不执行。
            stop = _comment_end(text, index, in_backticks=layer.opener == '`')
            layer.add(text[index:stop])
            index = stop
        elif text.startswith('$$', index):
            layer.add('$$')
            index += 2
        elif char == '$' and text[index + 1 : index + 2] == "'" and not layer.in_double:
            # `$'…'` 里的 `\'` 不结束字符串；当普通单引号扫，`$'\''` 之后整条命令都会落进引号。
            stop = index + 2
            while stop < len(text) and text[stop] != "'":
                stop += 2 if text[stop] == '\\' else 1
            layer.add(text[index : stop + 1])
            index = stop + 1
        elif text.startswith('$((', index):
            layer.add('$')
            index += 1
        elif text.startswith('$(', index):
            layers.append(_Layer('$('))
            index += 2
        elif char == '`':
            layers.append(_Layer('`'))
            index += 1
        elif text.startswith(_PROCESS_OPENERS, index) and not layer.in_double:
            layers.append(_Layer(text[index : index + 2]))
            index += 2
        else:
            if parenthesised and char == '(':
                layer.depth += 1
            elif parenthesised and char == ')':
                layer.depth -= 1
            layer.add(char)
            index += 1
    while len(layers) > 1:
        hit = _close_layer(layers, closed=False, handoffs=handoffs) or hit
    return ''.join(layers[0].text), hit


def _executes_a_download(tokens: Sequence[str]) -> bool:
    """这一段自己执行下载来的内容：它就是程序词、是 `eval` 的实参、是解释器 `-c` 这类选项交出的
    代码，或是解释器、`source`、`.` 读的脚本文件（进程替换）。

    交给本地脚本的普通实参（`python3 x.py "$(curl …)"`）不算：那是数据，与管道里的判据同一个理由。
    认不出选项的 wrapper 之后，第一个非选项词带着下载时它可能就是程序词（`xvfb-run $(curl …)`、
    `ionice -c2 $(curl …)`）；更靠后的是那个程序的实参（`flock -n /tmp/l ./sync.sh "$(curl …)"`），
    按数据算。
    """
    # 没有一个词带着下载，下面每一条都不可能成立；先挡掉，判定才不必对每一段都去找候选程序词。
    if not any(_carries_download(token) for token in tokens):
        return False
    programs = _determinate_programs(tokens)
    if any(_carries_download(tokens[index]) for index in programs):
        return True
    last = max(programs, default=None)
    if last is not None and _basename(tokens[last]) in _WRAPPERS:
        following = next(
            (
                token
                for token in tokens[last + 1 :]
                if not token.startswith('-') and not _ASSIGNMENT.match(token)
            ),
            None,
        )
        if following is not None and _carries_download(following):
            return True
    for index in _program_candidates(tokens):
        arguments = tokens[index + 1 :]
        name = _basename(tokens[index])
        if name == 'eval':
            if any(_carries_download(argument) for argument in arguments):
                return True
            continue
        if name in ('.', 'source'):
            operand = _first_operand(tokens, index + 1)
            if operand is not None and _DOWNLOADED_FILE in operand:
                return True
            continue
        interpreter = _interpreter(tokens[index])
        if interpreter is None:
            continue
        kind, word = _script_source(interpreter, arguments)
        if kind == 'option' and _carries_download(word):
            return True
        if kind == 'file' and _DOWNLOADED_FILE in word:
            return True
        if kind == 'unknown' and any(_carries_download(argument) for argument in arguments):
            return True
    return False


def _runs_downloaded_content(command: str) -> bool:
    """网络下载的内容被当成脚本执行：经管道进了一个从 stdin 读脚本的解释器，或经命令替换、进程
    替换成了解释器要执行的代码或脚本文件。

    管道链要按 `|` 断开重数：`curl x; sh y` 走的是文件、不是这条链，把 `;` 也算进来会让「下载过
    就算」扩散到整条命令行。中间隔多少段与判定无关——`| tee /tmp/a |` 挡不住后面那个 shell。
    重定向的目标里带着下载（`bash < <(curl …)`、`bash <<< "$(curl …)"`）时，从 stdin 读脚本的
    程序同样算。命令里有下载、又有接着解释器的输出进程替换（`curl … | tee >(sh)`、`curl -o >(sh)`）
    时也算：写进 `>(sh)` 的就是下载来的内容。后两条对整条命令判，不按管道链：多判一条牵强的组合，
    代价只是一次误报。

    已知盲区：带取值、而取值会被当代码加载的选项（`node -r <(curl …)`、
    `bash --rcfile <(curl …) -i`、`fish -C "$(curl …)"`）；解释器选项表里没有、又与 node 开关同前缀
    的带值选项；先跟一个位置实参的 wrapper（`flock /tmp/l $(curl …)`）；单独一个 `+`（`bash +`）；
    以及模块开头写明的那几类转手。

    转手执行的命令（`sh -c` 的代码、`eval` 的实参、`find -exec`）是一份新文本，从替换开始整个再扫
    一遍：单引号里的 `$(curl …)` 在这一层不执行，到了下一层才执行。层数上限与其余规则相同。
    """
    pending = deque([(command, 0)])
    seen = {command}
    while pending:
        text, depth = pending.popleft()
        handoffs: list[str] = []
        reduced, hit = _without_download_substitutions(text, handoffs)
        segments, targets = _parse(reduced, handoffs=False)
        if hit or _runs_parsed(segments, targets):
            return True
        handoffs.extend(code for _, tokens in segments for code in _handoffs(tokens))
        for code in handoffs:
            if code in seen:
                continue
            if depth == _MAX_HANDOFF_DEPTH:
                return True
            seen.add(code)
            pending.append((code, depth + 1))
    return False


def _feeds_a_sink(tokens: Sequence[str]) -> bool:
    """这一段把输出写进 `_SINK`：程序是 `tee`（`| tee >(sh)`）或下载器（`curl -o >(sh)`）。

    只看文本里有没有 `>(sh)` 不够：注释（`… | jq . # see: tee >(sh)`）里也会有。
    """
    return any(_SINK in token for token in tokens) and any(
        _basename(tokens[index]) == 'tee' or _basename(tokens[index]) in _DOWNLOADERS
        for index in _program_candidates(tokens)
    )


def _runs_parsed(segments: Sequence[tuple[bool, tuple[str, ...]]], targets: Sequence[str]) -> bool:
    """这条规则的全部判据，跑在替换已经换掉的一段命令上（整条命令，或其中的一层替换）。"""
    redirected = any(_carries_download(target) for target in targets)
    downloaded = False
    any_download = redirected
    for piped, tokens in segments:
        if not piped:
            downloaded = False
        fetches = _downloads(tokens) or any(_carries_download(token) for token in tokens)
        if (downloaded or redirected) and _reads_its_script_from_stdin(tokens):
            return True
        if _executes_a_download(tokens):
            return True
        if (downloaded or redirected or fetches) and _feeds_a_sink(tokens):
            return True
        if fetches:
            downloaded = True
            any_download = True
    return any_download and any(_SINK in target for target in targets)


def _removes_recursively_and_forcibly(command: str) -> bool:
    """`rm` 的标志里同时出现递归与强制。

    标志扫整段而不只扫命令词后面那几个：`rm /tmp/x -rf` 是合法的 GNU 写法。长选项按前缀认
    （`--rec` 就是 `--recursive`），短选项按字母认且不分大小写（`-R` 即递归）——不逐个枚举拼法，
    枚举漏一种就是漏一道闸，而多认几种的代价只是一次误报。

    `--` 之后停止扫标志：那之后的每个词都是文件名，`rm -- -rf` 删的是一个叫 `-rf` 的文件。
    """
    segments = _segments(command)
    if _too_deep(segments):
        return True
    for _, tokens in segments:
        if 'rm' not in _command_words(tokens):
            continue
        recursive = force = False
        for token in tokens:
            if token == '--':
                break
            if not token.startswith('-'):
                continue
            if token.startswith('--'):
                name = token[2:].split('=', 1)[0].lower()
                if not name:
                    continue
                recursive = recursive or 'recursive'.startswith(name)
                force = force or 'force'.startswith(name)
                continue
            cluster = token[1:].lower()
            recursive = recursive or 'r' in cluster
            force = force or 'f' in cluster
        if recursive and force:
            return True
    return False


def _writes_a_raw_device(command: str) -> bool:
    """重建文件系统，或把 `dd` 的输出指向 `/dev/` 下的一个真设备。

    `dd` 只认 `of=`：`if=/dev/sda` 是读设备，把它一并算上会让每一次取镜像都被阻断。写侧同样有
    无害目标——`of=/dev/null` 是量读吞吐的教科书写法，`/dev/stdout` 与 `/dev/tty` 只是把输出换个
    去处，它们都不碰块设备。
    """
    segments = _segments(command)
    if _too_deep(segments):
        return True
    for _, tokens in segments:
        words = _command_words(tokens)
        if any(word.startswith('mkfs') for word in words):
            return True
        if 'dd' not in words:
            continue
        for token in tokens:
            target = token.lower()
            if target.startswith('of=/dev/') and target[3:] not in _PSEUDO_DEVICES:
                return True
    return False


DEFAULT_RULES: tuple[DangerRule, ...] = (
    DangerRule(
        rule_id='shell.privilege_escalation',
        level=RiskLevel.HIGH,
        reason='这次调用以 sudo / doas 提权执行，作用范围超出当前用户能承担的后果',
        matches=_escalates_privilege,
    ),
    DangerRule(
        rule_id='shell.remote_script_execution',
        level=RiskLevel.HIGH,
        reason='这次调用把网络下载的内容直接交给解释器执行，执行前没有任何人看过它的内容',
        matches=_runs_downloaded_content,
    ),
    DangerRule(
        rule_id='shell.recursive_force_remove',
        level=RiskLevel.HIGH,
        reason='这次调用递归且强制删除，删掉的东西没有回收站也没有确认',
        matches=_removes_recursively_and_forcibly,
    ),
    DangerRule(
        rule_id='shell.raw_device_write',
        level=RiskLevel.HIGH,
        reason='这次调用直接写块设备或重建文件系统，一次成功就没有原状可回',
        matches=_writes_a_raw_device,
    ),
)


# --- 三族可选规则：宿主按需组合，不进 DEFAULT_RULES -----------------------------------------------
#
# 三族对「哪个词是程序」用两种相反的保守方向，这是承重的：
# - 找危险程序（git、rm）时宁可多判：每一个命令词槽、以及启动器展开出来的程序都算候选。只认一个
#   「真实程序词」会让每个没进 `_WRAPPERS` 的 wrapper 都成为绕过口。
# - 豁免程序词（越界判定）时宁可少判：只豁免确定的那一个程序词，认不出就不再往后豁免。


@dataclass(frozen=True)
class _Grammar:
    """一个 wrapper 或启动器的选项表。认不出的选项让解析失败，而不是猜它吃不吃下一个词。"""

    flags: frozenset[str] = frozenset()
    valued: frozenset[str] = frozenset()
    # 选项之后、程序词之前的位置实参个数（`timeout 60 cmd` 的 60）与它们的形状。形状不对就按认不出
    # 算：`timeout git push` 的 `git` 当成时长吞掉，程序词就成了 `push`。
    positionals: int = 0
    positional: re.Pattern[str] | None = None
    # 选项之后允许出现 `NAME=VALUE`（`env -i A=1 cmd`）。
    assignments: bool = False
    # `-19` 这样的纯数字选项（`nice -19 cmd`）。
    numeric: bool = False


@dataclass(frozen=True)
class _Launcher:
    """转发启动器：`before` 是子命令之前的选项，`after` 是子命令与被启动程序之间的选项。"""

    before: _Grammar
    subcommands: tuple[tuple[str, ...], ...]
    after: _Grammar = _Grammar()


def _skip_options(tokens: Sequence[str], start: int, grammar: _Grammar) -> int | None:
    """从 start 起跳过 grammar 认得的选项，交回第一个非选项的下标；选项认不出时交回 None。

    `--名字=值` 认不认得都跳过：它从不吃下一个词，跳过它不会把程序词当成取值吞掉。
    """
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token in grammar.flags:
            index += 1
        elif token == '--':
            return index + 1
        elif not token.startswith('-') or token == '-':
            return index
        elif token.startswith('--') and '=' in token:
            index += 1
        elif token in grammar.valued:
            index += 2
        elif not token.startswith('--') and token[:2] in grammar.valued:
            index += 1
        elif grammar.numeric and token[1:].isdigit():
            index += 1
        else:
            return None
    return len(tokens)


_WRAPPER_GRAMMARS: Mapping[str, _Grammar] = MappingProxyType(
    {
        'builtin': _Grammar(),
        'command': _Grammar(flags=frozenset({'-p', '-v', '-V'})),
        'env': _Grammar(
            flags=frozenset({'-', '-0', '-i', '-v', '--debug', '--ignore-environment', '--null'}),
            valued=frozenset({'-C', '-u', '--chdir', '--unset'}),
            assignments=True,
        ),
        'exec': _Grammar(flags=frozenset({'-c', '-l'}), valued=frozenset({'-a'})),
        'nice': _Grammar(valued=frozenset({'-n', '--adjustment'}), numeric=True),
        'nohup': _Grammar(),
        'setsid': _Grammar(flags=frozenset({'-c', '-f', '-w', '--ctty', '--fork', '--wait'})),
        'stdbuf': _Grammar(valued=frozenset({'-e', '-i', '-o', '--error', '--input', '--output'})),
        'time': _Grammar(
            flags=frozenset(
                {'-a', '-l', '-p', '-q', '-v', '--append', '--portability', '--quiet', '--verbose'}
            ),
            valued=frozenset({'-f', '-o', '--format', '--output'}),
        ),
        'timeout': _Grammar(
            flags=frozenset({'-v', '--foreground', '--preserve-status', '--verbose'}),
            valued=frozenset({'-k', '-s', '--kill-after', '--signal'}),
            positionals=1,
            positional=re.compile(r'[0-9]+(\.[0-9]*)?[smhd]?|\.[0-9]+[smhd]?'),
        ),
        # GNU 与 BSD 的并集。`-e` `-i` `-l` 只收紧贴的取值（`-i{}`），单独写时是开关；放进 valued
        # 就会把后面的程序词当取值吞掉，豁免落到它的实参上。
        'xargs': _Grammar(
            flags=frozenset({'-0', '-e', '-i', '-l', '-o', '-p', '-r', '-t', '-x', '--exit'})
            | {'--interactive', '--no-run-if-empty', '--null', '--open-tty', '--verbose'},
            valued=frozenset({'-a', '-d', '-E', '-I', '-J', '-L', '-n', '-P', '-R', '-S', '-s'})
            | {'--arg-file', '--delimiter', '--max-args', '--max-chars', '--max-lines'}
            | {'--max-procs'},
        ),
    }
)

_UV_GLOBAL = _Grammar(
    flags=frozenset(
        {'-n', '-q', '-v', '--no-cache', '--no-config', '--no-progress', '--offline', '--quiet'}
        | {'--verbose'}
    ),
    valued=frozenset({'--cache-dir', '--color', '--config-file', '--directory', '--project'}),
)
_UV_RUN = _Grammar(
    flags=_UV_GLOBAL.flags
    | {'-m', '-s', '--all-extras', '--frozen', '--isolated', '--locked', '--module', '--no-dev'}
    | {'--no-project', '--no-sync', '--script'},
    valued=_UV_GLOBAL.valued
    | {'-p', '--env-file', '--extra', '--group', '--index', '--package', '--python', '--with'}
    | {'--with-editable', '--with-requirements'},
)
_UVX = _Grammar(
    flags=_UV_GLOBAL.flags | {'-U', '--isolated', '--upgrade'},
    valued=_UV_GLOBAL.valued | {'-p', '--from', '--index', '--python', '--with'},
)
_POETRY = _Grammar(
    flags=frozenset({'-n', '-q', '-v', '--ansi', '--no-ansi', '--no-interaction', '--quiet'})
    | {'--verbose'},
    valued=frozenset({'-C', '-P', '--directory', '--project'}),
)
_PIPX_GLOBAL = _Grammar(flags=frozenset({'-q', '-v', '--quiet', '--verbose'}))
_PIPX_RUN = _Grammar(
    flags=_PIPX_GLOBAL.flags | {'-e', '--editable', '--no-cache', '--path', '--pypackages'},
    valued=frozenset({'--index-url', '--pip-args', '--python', '--spec'}),
)
_LAUNCHERS: Mapping[str, _Launcher] = MappingProxyType(
    {
        'uv': _Launcher(before=_UV_GLOBAL, subcommands=(('run',), ('tool', 'run')), after=_UV_RUN),
        'uvx': _Launcher(before=_UVX, subcommands=((),)),
        'poetry': _Launcher(before=_POETRY, subcommands=(('run',),), after=_POETRY),
        'pipx': _Launcher(before=_PIPX_GLOBAL, subcommands=(('run',),), after=_PIPX_RUN),
    }
)


def _wrapped_program(tokens: Sequence[str], index: int) -> int | None:
    """index 上的 wrapper 启动的程序词下标；不是有选项表的 wrapper、或选项认不出时是 None。"""
    grammar = _WRAPPER_GRAMMARS.get(_basename(tokens[index]))
    if grammar is None:
        return None
    position = _skip_options(tokens, index + 1, grammar)
    if position is None:
        return None
    if grammar.assignments:
        while position < len(tokens) and _ASSIGNMENT.match(tokens[position]):
            position += 1
    for word in tokens[position : position + grammar.positionals]:
        if grammar.positional is not None and not grammar.positional.fullmatch(word):
            return None
    position += grammar.positionals
    return position if position < len(tokens) else None


def _launched_program(tokens: Sequence[str], index: int) -> int | None:
    """index 上的启动器启动的程序词下标。

    不是启动器、或启动器后面没有程序时交回 `len(tokens)`；选项认不出时交回 None。
    """
    launcher = _LAUNCHERS.get(_basename(tokens[index]))
    if launcher is None:
        return len(tokens)
    position = _skip_options(tokens, index + 1, launcher.before)
    if position is None:
        return None
    for path in launcher.subcommands:
        if tuple(tokens[position : position + len(path)]) == path:
            return _skip_options(tokens, position + len(path), launcher.after)
    return len(tokens)


def _program_candidates(tokens: Sequence[str]) -> list[int]:
    """一段里**可能**是程序词的下标：每一个命令词槽，以及启动器展开出来的程序。

    启动器的选项认不出时，它后面每一个非选项词都算候选——这是找危险程序的方向，多判只是误报。
    """
    found = {index for index, _ in _command_word_slots(tokens)}
    pending = sorted(found)
    # 从这里到末尾的非选项词都已经是候选。每个选项认不出的启动器都要「后面全算」，只扫还没扫过的
    # 那一截：每次都从头扫，一串 `uv -X uv -X …` 就是平方时间，而判定跑在每一次工具调用上。
    swept = len(tokens)
    while pending:
        index = pending.pop()
        launched = _launched_program(tokens, index)
        if launched is None:
            more = [
                later
                for later in range(index + 1, swept)
                if not tokens[later].startswith('-') and not _ASSIGNMENT.match(tokens[later])
            ]
            swept = min(swept, index + 1)
        else:
            more = [later for later, _ in _command_word_slots(tokens, launched)]
        for later in more:
            if later not in found:
                found.add(later)
                pending.append(later)
    return sorted(found)


def _determinate_programs(tokens: Sequence[str]) -> set[int]:
    """一段里**确定**是程序词的下标：第一个词，以及认得出的 wrapper 一层层启动的程序词。

    认不出就停，之后的词全按实参看：`sudo`、`flock`、带着认不出的选项的 `timeout` 都是这样。
    启动器同样停在它自己——它启动的常常是一个脚本文件（`uv run /tmp/x.py`），与被拦下的
    `python3 /tmp/x.py` 是同一件事，豁免它就在那里开了一个口。
    """
    index = 0
    while index < len(tokens) and (
        _ASSIGNMENT.match(tokens[index]) or tokens[index] in _RESERVED_WORDS
    ):
        index += 1
    programs: set[int] = set()
    while index < len(tokens):
        programs.add(index)
        following = _wrapped_program(tokens, index)
        if following is None:
            break
        index = following
    return programs


_GIT_READ_ONLY = frozenset(
    {
        'blame',
        'cat-file',
        'describe',
        'diff',
        'grep',
        'help',
        'log',
        'ls-files',
        'ls-tree',
        'merge-base',
        'name-rev',
        'rev-parse',
        'shortlog',
        'show',
        'status',
        'version',
    }
)
# 可以跳过的全局选项。`-c`、`--config-env`、`--exec-path`、`--git-dir`、`--work-tree` 刻意不在
# 这几张表里：它们让只读子命令换一份配置、换一个仓库或换一套可执行文件（`-c core.fsmonitor=<命令>`
# 让 `status` 执行任意命令），于是落到「认不出」那一支，出现即按改状态算。
_GIT_VALUED = frozenset({'-C', '--namespace'})
_GIT_EQUALS_ONLY = frozenset({'--attr-source', '--list-cmds'})
# `--bare` 不在这里：它等于 `--git-dir=.`，让当前目录里的 `config`（及其中的别名）生效。
_GIT_FLAGS = frozenset(
    {
        '-P',
        '-h',
        '-p',
        '-v',
        '--glob-pathspecs',
        '--help',
        '--html-path',
        '--icase-pathspecs',
        '--info-path',
        '--literal-pathspecs',
        '--man-path',
        '--no-advice',
        '--no-lazy-fetch',
        '--no-optional-locks',
        '--no-pager',
        '--no-replace-objects',
        '--noglob-pathspecs',
        '--paginate',
        '--version',
    }
)
_GIT_ASSIGNMENT = re.compile(r'\AGIT_[A-Za-z0-9_]*\+?=')
_GLOB = frozenset('*?[')


def _opens_files_with_a_command(arguments: Sequence[str]) -> bool:
    """`git grep` 的 `-O<命令>` / `--open-files-in-pager`：只读子命令里唯一自带执行命令的选项。

    短选项可以成簇（`-nOvim`），长选项可以缩写到不歧义为止（`--op`）。
    """
    for word in arguments:
        if word.startswith('--'):
            name = word.split('=', 1)[0]
            if len(name) >= len('--op') and '--open-files-in-pager'.startswith(name):
                return True
        elif word.startswith('-') and 'O' in word[1:]:
            return True
    return False


def _git_changes_state(tokens: Sequence[str], index: int) -> bool:
    """tokens[index] 是 git 时，这次调用会不会改 git 状态。认不出的全局选项按改状态算。"""
    position = index + 1
    while position < len(tokens):
        token = tokens[position]
        if not token.startswith('-') or token == '-':
            break
        name = token.split('=', 1)[0]
        if token in _GIT_FLAGS or ('=' in token and name in _GIT_VALUED | _GIT_EQUALS_ONLY):
            position += 1
        elif token in _GIT_VALUED:
            position += 2
        else:
            return True
    else:
        return False
    subcommand = tokens[position]
    if subcommand not in _GIT_READ_ONLY:
        return True
    return subcommand == 'grep' and _opens_files_with_a_command(tokens[position + 1 :])


def _names_git_dir(path: str) -> bool:
    """路径的某一段是 `.git`（不分大小写），或是一个会展开出 `.git` 的通配段（`.git*`、`.*`）。

    通配段只在以 `.` 或 `[` 开头时才可能匹配 `.git`：shell 的 `*` 不匹配开头的点，`rm -rf *`
    碰不到 `.git`。
    """
    for part in path.split('/'):
        folded = part.casefold()
        if folded == '.git':
            return True
        if (
            folded[:1] in ('.', '[')
            and not _GLOB.isdisjoint(folded)
            and fnmatch.fnmatchcase('.git', folded)
        ):
            return True
    return False


def _changes_git_state(command: str) -> bool:
    segments, targets = _parse(command, thorough=True)
    if _too_deep(segments) or any(_names_git_dir(target) for target in targets):
        return True
    for _, tokens in segments:
        for token in tokens:
            value = token.split('=', 1)[1] if '=' in token else ''
            # 紧贴在短选项后面的取值（`curl -o.git/hooks/x`）同样是一个路径。
            attached = token[2:] if token.startswith('-') and not token.startswith('--') else ''
            if _GIT_ASSIGNMENT.match(token) or any(
                _names_git_dir(part) for part in (token, value, attached)
            ):
                return True
        for index in _program_candidates(tokens):
            word = _basename(tokens[index])
            if word == 'git' and _git_changes_state(tokens, index):
                return True
            # `git-core/git-push` 这类带连字符的可执行文件：连字符后面就是子命令。
            if word.startswith('git-') and word[4:] not in _GIT_READ_ONLY:
                return True
    return False


def _within(path: str, root: str) -> bool:
    """规范化后的 path 是 root 本身或在它之下。按路径段比，不按前缀：`/a/bc` 不在 `/a/b` 里。"""
    return path == root or path.startswith(root.rstrip('/') + '/')


_FD_DEVICE = re.compile(r'/dev/fd/[0-9]+')


def _without_pwd(word: str) -> str:
    """`$PWD` / `${PWD}` 开头的词换成 `.` 开头：它就是这条命令的工作目录。"""
    for prefix in ('${PWD}', '$PWD'):
        if word == prefix or word.startswith(prefix + '/'):
            return '.' + word[len(prefix) :]
    return word


def _placements(root: str, word: str, shallowest: int) -> list[str]:
    """相对的 word 在「Bash 的 cwd 是根下任意一层」时可能落到的绝对路径（规范化）。

    只有开头那串 `..` 爬得出 cwd，所以 cwd 的深度只需从 0 试到 `..` 的个数：更深的 cwd 让它停在
    根下面。第 k 个落点是从根往上 k 层再接上剩下的部分；`shallowest` 是从第几个开始要，0 就是
    cwd 恰好在根上、word 原样接在根下。从根出发看不越界的 `../../agents/proj/x`，cwd 在 `src`
    时落在 `/srv/agents/agents/proj/x`——只从根算会漏掉它。
    """
    parts = posixpath.normpath(word).split('/')
    ups = next((index for index, part in enumerate(parts) if part != '..'), len(parts))
    rest = '/'.join(parts[ups:])
    placements: list[str] = []
    base = root
    for level in range(ups + 1):
        if level >= shallowest:
            placements.append(posixpath.normpath(posixpath.join(base, rest)) if rest else base)
        base = posixpath.dirname(base)
    return placements


def _covers(root: str, path: str) -> bool:
    """删掉 path 会不会连 root 一起删（两者都已规范化、折叠大小写）。

    逐段比：path 的每一段都等于、或作为通配匹配 root 同一深度的那一段，path 就是 root 或它的祖先
    （`/srv/*/proj` 也算）；比到 root 之下还剩一段时，它得是一个「全部」的通配（`root/*`）。
    """
    root_parts = [part for part in root.split('/') if part]
    path_parts = [part for part in path.split('/') if part]
    for depth, part in enumerate(path_parts):
        if depth == len(root_parts):
            return depth == len(path_parts) - 1 and _matches_everything(part)
        if part != root_parts[depth] and (
            _GLOB.isdisjoint(part) or not fnmatch.fnmatchcase(root_parts[depth], part)
        ):
            return False
    return True


def _matches_everything(pattern: str) -> bool:
    """根下的这个通配算不算「全部」：连 `a` 与 `z` 这样的名字都匹配（`*`、`?*`、`[!.]*`、
    `[a-z]*`）。`*.pyc`、`build*` 不算。"""
    return not _GLOB.isdisjoint(pattern) and all(
        fnmatch.fnmatchcase(probe, pattern) for probe in ('a', 'z')
    )


def _path_words(token: str) -> tuple[str, ...]:
    """一个实参里要按路径看的部分：它自己，以及 `--选项=值` 的值。"""
    if token.startswith('--') and '=' in token:
        return (token, token.split('=', 1)[1])
    return (token,)


def _cd_leaves(arguments: Sequence[str]) -> bool:
    """`cd` 不带目录（回 `$HOME`）或 `cd -`（回上一个目录）：去向都不在命令文本里。"""
    operands = [word for word in arguments if word == '-' or not word.startswith('-')]
    return not operands or operands[0] == '-'


def _rm_operands(arguments: Sequence[str]) -> list[str]:
    operands: list[str] = []
    options_done = False
    for word in arguments:
        if not options_done and word == '--':
            options_done = True
        elif options_done or not word.startswith('-') or word == '-':
            operands.append(word)
    return operands


# 起点之前的选项：GNU 的 `-H` `-L` `-P` `-D 调试项` `-O级别`，BSD 的 `-E` `-X` `-d` `-s` `-x` 与
# `-f 起点`。
_FIND_LEADING_FLAGS = frozenset({'-E', '-H', '-L', '-P', '-X', '-d', '-s', '-x'})
_FIND_OPTIMISATION = re.compile(r'-O[0-9]*')
# 收窄的测试：只对一部分文件成立。名单之外的测试按不收窄算：漏认一个收窄只是一次误报，多认一个
# 就是删根放行。除 `_FIND_NULLARY_TESTS` 之外都带一个取值；`-newerXY` 按 `_FIND_NEWER` 认。
_FIND_TESTS = frozenset(
    {'-Bmin', '-Bnewer', '-Btime'}
    | {'-amin', '-anewer', '-atime', '-cmin', '-cnewer', '-ctime', '-empty', '-executable'}
    | {'-false', '-fstype', '-gid', '-group', '-ilname', '-iname', '-inum', '-ipath', '-iregex'}
    | {'-iwholename', '-links', '-lname', '-mmin', '-mtime', '-name', '-newer', '-nogroup'}
    | {'-nouser', '-path', '-perm', '-readable', '-regex', '-samefile', '-size', '-type', '-uid'}
    | {'-used', '-user', '-wholename', '-writable', '-xtype'}
)
_FIND_NULLARY_TESTS = frozenset(
    {'-empty', '-executable', '-false', '-nogroup', '-nouser', '-readable', '-writable'}
)
_FIND_NEWER = re.compile(r'-newer[aBcmt][aBcmt]')
# 不收窄、不删除，但吃掉后面几个词的选项与动作；没列的按不吃词算。
_FIND_ARITY: Mapping[str, int] = MappingProxyType(
    {
        '-files0-from': 1,
        '-fls': 1,
        '-fprint': 1,
        '-fprint0': 1,
        '-fprintf': 2,
        '-maxdepth': 1,
        '-mindepth': 1,
        '-printf': 1,
        '-regextype': 1,
    }
)
_FIND_OR = frozenset({'-o', '-or', ','})
_FIND_NOT = frozenset({'!', '-not'})
# 对谁都成立的 `-regex` 只认这几种写法。从命令里取来的正则不交给正则引擎：模式与起点都由模型定，
# 一个嵌套量词（`(a+)+`）就让回溯成指数时间，而判定跑在每一次工具调用上。
_EVERY_PATH_REGEX = frozenset({'.*', '^.*', '.*$', '^.*$', '.+', '^.+$', '..*', '.*/.*', '\\./.*'})
# 括号与取反的嵌套层数上限，超过就按删光算：层数由模型决定，递归下去会撞 RecursionError。
_MAX_FIND_NESTING = 64
_FIND_PATH_TESTS = frozenset({'-path', '-ipath', '-wholename', '-iwholename'})
# 有路径测试时逐个起点判的起点数上限：逐个判是起点数乘表达式长度。起点再多，路径测试就一律按
# 不收窄算，宁可多判。
_MAX_PROBED_STARTS = 8


def _wholly_deleted_starting_points(arguments: Sequence[str]) -> list[str]:
    """`find` 整个删掉的起点：表达式里有一次删除落在（几乎）全部文件上，见 `_FindExpression`。
    没写起点时是 `.`。删的不是全部时交回空表。"""
    points: list[str] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in _FIND_LEADING_FLAGS or _FIND_OPTIMISATION.fullmatch(word):
            index += 1
        elif word in ('-D', '-f'):
            if word == '-f':
                points.append(_next_word(arguments, index + 1))
            index += 2
        else:
            break
    while index < len(arguments) and not (
        arguments[index].startswith('-') or arguments[index] in ('(', '!', ')', ',')
    ):
        points.append(arguments[index])
        index += 1
    starts = points or ['.']
    expression = arguments[index:]
    if len(starts) > _MAX_PROBED_STARTS or not any(word in _FIND_PATH_TESTS for word in expression):
        return starts if _FindExpression(expression, None).deletes_wholesale() else []
    # 路径测试对不同的起点答案不同（`find other . -path 'other/*'` 删的只是 other 下的）：
    # 逐个起点判。
    return [start for start in starts if _FindExpression(expression, start).deletes_wholesale()]


class _TooNested(Exception):
    """find 表达式的嵌套超过 `_MAX_FIND_NESTING`。内部信号。"""


class _FindExpression:
    """find 的表达式里，有没有一次删除（`-delete`，或执行带 `{}` 的 `rm`）落在起点下（几乎）全部
    文件上。

    按 find 的求值规则：一次删除只作用于它所在的每一层「与」链在它之前都为真的文件。链里有一个
    收窄的测试（`-name '*.pyc'`），删的就只是筛出来的那些。`-o` 与 `,` 断开链：`-o` 之后那一支看到的
    是前面没选中的文件，仍然几乎是全部。括号算一个原语，每一支都收窄它才算收窄。取反的测试
    （`! -name keep`）选中的是「其余全部」，对谁都成立的测试（`-name '*'`）也不算收窄。`-exec … ;`
    里的词是那条命令的实参，不是测试。每一次删除都要看，不只第一次。
    """

    def __init__(self, tokens: Sequence[str], start: str | None) -> None:
        """`start` 是路径测试要对着判的那一个起点；None 时路径测试一律按不收窄算。"""
        self._tokens = tokens
        self._start = start
        self._index = 0
        self._depth = 0

    def deletes_wholesale(self) -> bool:
        try:
            found = False
            while self._index < len(self._tokens):
                _, deletes = self._alternatives(narrowed=False)
                found = found or deletes
                # 多出来的 `)`：跳过它接着读，后面的删除照样要看。
                self._index += 1
            return found
        except _TooNested:
            return True

    def _peek(self) -> str | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _alternatives(self, *, narrowed: bool) -> tuple[bool, bool]:
        """(每一支都收窄, 有一次删除落在全部上)。每一支看到的都是外层链的范围。"""
        every_branch_narrows = True
        deletes = False
        while True:
            narrows, branch_deletes = self._chain(narrowed=narrowed)
            every_branch_narrows = every_branch_narrows and narrows
            deletes = deletes or branch_deletes
            if self._peek() not in _FIND_OR:
                return every_branch_narrows, deletes
            self._index += 1

    def _chain(self, *, narrowed: bool) -> tuple[bool, bool]:
        deletes = False
        while (word := self._peek()) is not None and word not in _FIND_OR and word != ')':
            # `-a` / `-and` 落到不收窄、不吃词的原语那一支，与省略它一样。
            narrows, primary_deletes = self._primary(narrowed=narrowed)
            deletes = deletes or primary_deletes
            narrowed = narrowed or narrows
        return narrowed, deletes

    def _primary(self, *, narrowed: bool) -> tuple[bool, bool]:
        """(这个原语收窄, 它里面有一次删除落在全部上)。"""
        word = self._tokens[self._index]
        self._index += 1
        if word in _FIND_NOT or word == '(':
            self._depth += 1
            if self._depth > _MAX_FIND_NESTING:
                raise _TooNested
            try:
                if word == '(':
                    narrows, deletes = self._alternatives(narrowed=narrowed)
                    if self._peek() == ')':
                        self._index += 1
                    return narrows, deletes
                if self._peek() is None:
                    return False, False
                return False, self._primary(narrowed=narrowed)[1]
            finally:
                self._depth -= 1
        if word == '-delete':
            return False, not narrowed
        if word in _FIND_ACTIONS:
            start = self._index
            while self._index < len(self._tokens) and not _ends_find_command(
                self._tokens, self._index
            ):
                self._index += 1
            command = self._tokens[start : self._index]
            self._index += 1
            return False, not narrowed and _removes_found(command)
        if word in _FIND_TESTS or _FIND_NEWER.fullmatch(word):
            if word in _FIND_NULLARY_TESTS:
                return True, False
            argument = self._peek() or ''
            self._index += 1
            return not self._selects_everything(word, argument), False
        self._index += _FIND_ARITY.get(word, 0)
        return False, False

    def _selects_everything(self, test: str, pattern: str) -> bool:
        """对起点下每个文件都成立的测试：`-name '*'`、`-path '*'`、`-path './*'`、`-regex '.*'`。
        `-lname '*'` 不算：它只对符号链接成立。"""
        folded = test in ('-iname', '-ipath', '-iwholename')
        if test in ('-name', '-iname'):
            return _matches_everything(pattern.casefold() if folded else pattern)
        if test in ('-regex', '-iregex'):
            return pattern in _EVERY_PATH_REGEX
        if test not in _FIND_PATH_TESTS:
            return False
        if self._start is None:
            return True
        probes = [f'{self._start.rstrip("/") or "/"}/{name}'.replace('//', '/') for name in 'az']
        return all(
            fnmatch.fnmatchcase(probe.casefold(), pattern.casefold())
            if folded
            else fnmatch.fnmatchcase(probe, pattern)
            for probe in probes
        )


def _removes_found(command: Sequence[str]) -> bool:
    """`find` 的动作执行的是 `rm`，实参里有 `{}`（找到的那个文件）。只看第一个 `rm` 候选：它之后的
    词都是它的操作数，后面那些候选的操作数是它的一部分。"""
    for index in _program_candidates(command):
        if _basename(command[index]) == 'rm':
            return any('{}' in operand for operand in _rm_operands(command[index + 1 :]))
    return False


@dataclass(frozen=True)
class _WorkspaceRoot:
    """构造规则时解析一次的工作区根。`matches` 只在这两个字符串上做字符串运算，不碰磁盘。

    同时留字面形态与解析掉符号链接之后的形态：macOS 上 `/tmp/x` 解析成 `/private/tmp/x`，命令里
    两种写法都会出现，只认一种就把另一种判成工作区外（越界）或判不出是根（删根）。
    """

    lexical: str
    real: str

    @classmethod
    def of(cls, root: object, factory: str, real: object = None) -> _WorkspaceRoot:
        """`real` 给出时就用它当解析之后的根，不在这台机器上解析：回放的机器上同一个路径可能解析成
        别处，判定就与记录的对不上（M3 D11）。"""
        lexical = cls._absolute(root, factory, 'root')
        if real is not None:
            return cls(lexical=lexical, real=cls._absolute(real, factory, 'real'))
        try:
            real = str(Path(lexical).resolve())
        except (OSError, RuntimeError) as exc:
            raise ConfigError(
                expected=f'{factory} 的 root 能解析掉符号链接',
                received=f'解析 {lexical!r} 时 {type(exc).__name__}: {exc}',
                next_action='检查工作区根路径上的符号链接（是否成环、是否有权限读），修好后再构造规则',
            ) from exc
        return cls(lexical=lexical, real=real)

    @staticmethod
    def _absolute(value: object, factory: str, name: str) -> str:
        shown, kind = repr(value), type(value).__name__
        text: object = None
        if isinstance(value, str | os.PathLike):
            text = os.fspath(cast('str | os.PathLike[str]', value))
        if not isinstance(text, str) or not text.startswith('/') or '\0' in text:
            raise ConfigError(
                expected=f'{factory} 的 {name} 是工作区根的绝对路径（str 或 os.PathLike）',
                received=f'收到 {shown}，类型是 {kind}',
                next_action=(
                    '传记录下的、原始机器上解析出的工作区根（trace 的宿主档案里有）；'
                    '不要在这台机器上重新解析，real= 就是为了不解析才有的'
                    if name == 'real'
                    else f'宿主先把工作区根解析成绝对路径再交给 {factory}'
                    '（例如 Path(root).resolve()）；规则不读 cwd，相对路径没有可以相对的地方'
                ),
            )
        return posixpath.normpath(text)

    def points_outside(self, word: str) -> bool:
        word = _without_pwd(word)
        if word.startswith('~'):
            return True
        if word in ('$HOME', '${HOME}') or word.startswith(('$HOME/', '${HOME}/')):
            return True
        if word.startswith('/'):
            normalized = posixpath.normpath(word)
            if normalized in _PSEUDO_DEVICES or _FD_DEVICE.fullmatch(normalized):
                return False
            return not any(_within(normalized, root) for root in (self.lexical, self.real))
        # 相对路径由内核从 cwd 的真实目录解析 `..`，所以只接在解析掉符号链接的根上。
        return any(
            not _within(placement, self.real) for placement in _placements(self.real, word, 1)
        )

    def reaches_outside(self, command: str) -> bool:
        segments, targets = _parse(command, thorough=True)
        if _too_deep(segments) or any(self.points_outside(target) for target in targets):
            return True
        for _, tokens in segments:
            programs = _determinate_programs(tokens)
            if not programs:
                continue
            program = max(programs)
            word = _basename(tokens[program])
            if word in ('pushd', 'popd'):
                return True
            if word == 'cd' and _cd_leaves(tokens[program + 1 :]):
                return True
            for index in range(min(programs), len(tokens)):
                token = tokens[index]
                if index in programs:
                    # 确定的程序词只在是绝对路径时放行（`/usr/bin/python3`）；`~/bin/x`、
                    # `../other/run.sh` 执行的是工作区外的文件，与 `sh ../other/run.sh` 同一件事。
                    if not token.startswith('/') and self.points_outside(token):
                        return True
                elif any(self.points_outside(part) for part in _path_words(token)):
                    return True
        return False

    def covers_root(self, operand: str) -> bool:
        """rm 这个目标会不会连根一起删：是根、是根的祖先，或逐段通配能匹配到它们，或是根下的
        「全部」。相对目标按 Bash 的 cwd 在根下任意一层试（`_placements`），接在两种形态的根上。

        不分大小写：APFS 默认不区分，`rm -r /USERS/...` 删的就是根；多判的代价只是一次误报。
        """
        word = _without_pwd(operand)
        if not word:
            return False
        for root in (self.lexical, self.real):
            placements = (
                [posixpath.normpath(word)] if word.startswith('/') else _placements(root, word, 0)
            )
            if any(_covers(root.casefold(), placement.casefold()) for placement in placements):
                return True
        return False

    def removes_root(self, command: str) -> bool:
        segments = _parse(command, thorough=True)[0]
        if _too_deep(segments):
            return True
        for _, tokens in segments:
            # 每种程序只看第一个候选：wrapper 之后每个词都是候选，后面那些同名候选的操作数是第一个的
            # 一部分；每个都重扫一遍就是平方，而这条规则在开发期跑在每一次工具调用上。
            looked: set[str] = set()
            for index in _program_candidates(tokens):
                name = _basename(tokens[index])
                if name not in ('rm', 'find') or name in looked:
                    continue
                looked.add(name)
                arguments = tokens[index + 1 :]
                if name == 'rm':
                    operands = _rm_operands(arguments)
                else:
                    operands = _wholly_deleted_starting_points(arguments)
                if any(self.covers_root(operand) for operand in operands):
                    return True
        return False


def git_state_change_rule() -> DangerRule:
    """`shell.git_state_change`：这条命令会改 git 状态。

    命中：git 的子命令不在只读集合里（`git-push` 这类带连字符的可执行文件同样按子命令算）；
    `-c`、`--config-env`、`--exec-path`、`--git-dir`、`--work-tree`、`--bare` 与认不出的全局选项；
    `grep` 用 `-O` / `--open-files-in-pager` 执行命令；`GIT_` 开头的环境赋值（任何位置，`export`
    也算）；任何实参、`=` 之后的值、紧贴短选项的取值（`-o.git/x`）或重定向目标里有一段是 `.git`
    （不分大小写，含会展开出它的通配）。git 按命令词槽与启动器展开出来的程序找（`setsid git push`、
    `uv run --with rich git push`、`{ git push; }`、双引号里的 `$(git push)`），转手执行的命令里
    同样找（`sh -c 'git push'`、`eval`、`xargs git add`、`find … -exec git add {} +`）。

    已知误报：`find . -path ./.git -prune` 这类只读引用 `.git` 的命令；只读集合之外的只读用法
    （`git branch`、`git stash list`、`git remote -v`、`git config --get`）；`echo GIT_DIR=x`；
    没有选项表、或有认不出的选项的 wrapper（`sudo`、`flock`）之后恰好写着 `git` 的普通实参；结束
    标记不带引号、喂进管道或交给解释器的 heredoc 正文。
    已知盲区：`python -c "…"`、`make`、脚本文件里的 git；模块开头写明的那几类转手；变量
    （`$GIT push`）；花括号展开（`{.git,x}`）；名单之外的启动器与 wrapper（`xcrun`、`pdm run`）。
    反斜杠与 `$'…'` 转义按 shell 的规则消掉（`g\\it push`、`$'\\x67it' push` 都命中）。
    """
    return DangerRule(
        rule_id='shell.git_state_change',
        level=RiskLevel.HIGH,
        reason=(
            '这次调用会改变 git 状态（提交、推送、改引用或配置、写 .git/，或换一份 git 配置执行）；'
            '提交与推送由宿主在复核之后执行'
        ),
        matches=_changes_git_state,
    )


def path_outside_workspace_rule(root: object, *, real: object = None) -> DangerRule:
    """`shell.path_outside_workspace`：实参或重定向目标指向工作区之外。

    `root` 是工作区根的绝对路径，构造时解析一次；`real` 给出时用它当解析之后的根、不再解析（回放用
    记录下来的那一份）。命中：工作区外的绝对路径（`_PSEUDO_DEVICES`
    与 `/dev/fd/N` 除外）、`~` 开头、`$HOME` / `${HOME}` 开头、会逃出去的 `..`（`$PWD` 当 `.`）；
    `--选项=值` 按值判；不带目录的 `cd`、`cd -`、`pushd`、`popd`（`builtin cd`、`{ cd; }` 也算）。
    包含关系按规范化后的路径段比，兄弟目录 `<根>-other` 在外面。相对路径按 Bash 的 `cwd` 在根下
    任意一层都试一遍，接在解析掉符号链接的根上：从根看不越界的 `../../agents/proj/x`，cwd 在
    `src` 时就在外面。

    每段只豁免**确定的**程序词：第一个词，以及有选项表的 wrapper（`timeout 60`、`env -i`、
    `nice -n 5` 等）启动的程序，并且只在它是绝对路径时豁免；认不出就不再往后豁免。
    `timeout 60 cat /etc/passwd`、`setsid cat /etc/passwd`、`~/bin/x.sh`、`../other/run.sh` 因此
    都命中，`/usr/bin/python3 x.py` 不命中。转手执行的命令（`sh -c 'cat /etc/hosts'`、`eval`、
    `find -exec`）按同样的规则判。

    已知盲区：`make`、脚本文件与非 shell 解释器代码里的路径；模块开头写明的那几类转手；变量与
    未加引号的命令替换（`$(pwd)`、`${HOME%/}`）；选项与取值粘在一起的写法（`-o/etc/x`）；
    `dd of=` 这类不带 `--` 的 `名字=值`；工作区里指向外面的符号链接；以绝对路径给出的工作区外
    可执行文件（`/tmp/x.sh`）。
    已知误报：以 `/` 开头的 sed / awk 脚本与搜索模式（`sed -n '/def x/,/^$/p'`）、`/tmp` 下的临时
    文件、`echo $HOME`、`cd src && … && cd ..`（命令内的 `cd` 不跟踪）、结束标记不带引号的 heredoc
    正文里的路径。
    """
    workspace = _WorkspaceRoot.of(root, 'path_outside_workspace_rule', real)
    return DangerRule(
        rule_id='shell.path_outside_workspace',
        level=RiskLevel.HIGH,
        reason=(
            '这次调用读写工作区之外的路径（绝对路径、~、$HOME、逃出根目录的 ..，'
            '或离开工作区的 cd），超出了这次任务的范围'
        ),
        matches=workspace.reaches_outside,
        params=(('root', workspace.lexical), ('real', workspace.real)),
    )


def workspace_root_removal_rule(root: object, *, real: object = None) -> DangerRule:
    """`shell.workspace_root_removal`：`rm` 或 `find` 删掉工作区根本身、根下的全部内容，或根的祖先。

    `root` 与 `real` 同 `path_outside_workspace_rule`。命中的目标规范化之后是根（`.`、`./`、`.//`、
    `src/..`、`$PWD`、根的绝对路径、`../<根目录名>`）、根的祖先（`..`、`/`）、逐段通配能匹配到
    根或其祖先（`/srv/*/proj`、`../*`），或根下的「全部」（`*`、`./*`、`?*`、`[!.]*`）。相对目标
    按 Bash 的 `cwd` 在根下任意一层都试（cwd 在 `src` 时 `../../proj` 就是根），接在字面与解析掉
    符号链接的两种根上。不看 `-r` / `-f`：`rm *` 不带 `-r` 也删光根下所有文件。`find` 的目标是
    它的起点（没写时是 `.`），只在有一次删除（`-delete`，或执行带 `{}` 的 `rm`）落在起点下几乎全部
    文件上时算：按 find 的求值规则，它所在的「与」链在它之前没有收窄的测试（`-name`、`-type`、
    `-mtime` 等；取反的、对谁都成立的如 `-name '*'` 不算），见 `_FindExpression`。rm 与 find 按命令
    词槽、启动器展开出来的程序与转手执行的命令找（`sh -c 'rm -rf .'`）。`rm -r build`、`rm *.pyc`、
    `rm -r ../build`、`find . -name '*.pyc' -delete`、`find . \\( -name a -o -name b \\) -delete`、
    `find . -path ./x -prune -o -name '*.js' -delete` 不命中。

    已知误报：连 `a` 与 `z` 都匹配的通配按「全部」算（`rm -r ?`、`find . -name '[a-z]*' -delete`、
    `-path './?'`）；cwd 就在根上时的 `rm -r ../../<根目录名>`（删的是根外那个同名目录，照样按删根
    拦下）；名单之外的 find 测试（`-context`）不算收窄，双重取反（`! ! -name x`）也不算；有路径测试
    又有 8 个以上起点时，路径测试一律不算收窄。已知盲区：`~` 与 `$PWD` 以外的变量不展开；`$(pwd)`
    与反引号；`xargs rm`（目标从 stdin 来）、经 shell 转手删找到的文件
    （`find -exec sh -c 'rm "$1"' _ {} \\;`）、`make`；`-regex` 只认 `_EVERY_PATH_REGEX` 里那几种
    「全部」的写法，别的都算收窄；花括号展开（`{*,.*}`）；`git clean`（它归
    `shell.git_state_change`）。
    """
    workspace = _WorkspaceRoot.of(root, 'workspace_root_removal_rule', real)
    return DangerRule(
        rule_id='shell.workspace_root_removal',
        level=RiskLevel.HIGH,
        reason='这次调用删除工作区根目录本身或根下的全部内容，删掉的东西没有回收站',
        matches=workspace.removes_root,
        params=(('root', workspace.lexical), ('real', workspace.real)),
    )


class _TooMuchText(Exception):
    """参数值读不完。内部信号，由 `_readable_texts` 转成对外那条带三件套的错误。"""

    def __init__(self, limit: str) -> None:
        super().__init__(limit)
        self.limit = limit


def _texts(value: object) -> list[str]:
    """一个参数值里所有能当命令读的文本。

    序列既交出拼接串**也**交出每个元素：`['rm', '-rf', '/']` 是一条 argv，只看元素时每一段都无害；
    `['ls', 'rm -rf /']` 是一串独立命令，只看拼接串时 `rm` 不在命令词位置上。两种形态都自然，少
    交一种就是对那一种没有闸。

    集合不拼接：拼接顺序随迭代顺序变，同样的输入会给出不同的文本，而判定必须可复现。

    **深度与总量都有上限，超了就抛。** 拼接串本身是全部子文本之和，于是每嵌一层字符量翻倍：67
    字节的自嵌套列表在 24 层上要跑十几秒、30 层上是分钟与 GB 级。参数完全由模型控制，而判定坐在
    每次工具调用的必经之路上，没有上限时一次畸形入参就是一次停机。深度上限同时兜住自引用容器
    （否则 `RecursionError` 会逃出判定）。
    """
    remaining = _MAX_TEXT_CHARS

    def charge(text: str) -> str:
        nonlocal remaining
        remaining -= len(text)
        if remaining < 0:
            raise _TooMuchText(f'读出的文本总量超过 {_MAX_TEXT_CHARS} 字符')
        return text

    def collect(item: object, depth: int) -> list[str]:
        if depth > _MAX_TEXT_DEPTH:
            raise _TooMuchText(f'容器嵌套超过 {_MAX_TEXT_DEPTH} 层')
        if isinstance(item, str):
            return [charge(item)]
        if isinstance(item, bytes | bytearray):
            return [charge(bytes(item).decode('utf-8', errors='replace'))]
        if isinstance(item, Mapping):
            mapping = cast('Mapping[Any, Any]', item)
            return [text for member in mapping.values() for text in collect(member, depth + 1)]
        if isinstance(item, AbstractSet):
            members = cast('AbstractSet[Any]', item)
            return [text for member in members for text in collect(member, depth + 1)]
        if isinstance(item, Sequence):
            items = cast('Sequence[Any]', item)
            parts = [text for member in items for text in collect(member, depth + 1)]
            return [charge(' '.join(parts)), *parts]
        return []

    return collect(value, 0)


def _readable_texts(tool: Tool, name: str, value: object) -> list[str]:
    try:
        return _texts(value)
    except _TooMuchText as exc:
        raise ContractViolation(
            expected=(
                f'{type(tool).__name__} 的参数 {name!r} 在 {_MAX_TEXT_DEPTH} 层嵌套、'
                f'{_MAX_TEXT_CHARS} 字符以内读得完'
            ),
            received=f'{exc.limit}，判定没能读完它',
            next_action=(
                f'把 {name!r} 传成一条命令文本或一层 argv 序列；'
                '读不完的值判定给不出结论，而按「没有规则命中」放行等于那道闸不存在'
            ),
        ) from exc


def _readable_as_text(value: object) -> bool:
    return value is None or isinstance(
        value, str | bytes | bytearray | Mapping | Sequence | AbstractSet
    )


def _declared_command_args(tool: Tool) -> tuple[str, ...] | None:
    """取出并校验 `__tool_command_args__`。

    声明的名字必须是 `execute` 收得到的参数名：名字写错时那条声明落在一个永远不存在的 key 上，
    判定于是一句不说地什么都不检查——闸消失得无声无息，比报错难查得多。`**kwargs` 收任意名字，
    此时没有可对照的名单，一律接受；不带 `functools.wraps` 的装饰器会把签名塌成 `(*a, **k)`，
    于是这条豁免也会落在一个本来有名单的工具上，拼错的名字照样被接受。
    """
    declared = cast('object', tool.__tool_command_args__)
    if declared is None:
        return None
    shown, kind = repr(declared), type(declared).__name__
    if (
        isinstance(declared, str)
        or not isinstance(declared, Sequence)
        or any(not isinstance(name, str) for name in cast('Sequence[Any]', declared))
    ):
        raise ConfigError(
            expected=(
                f'{type(tool).__name__}.__tool_command_args__ 是一个参数名元组，'
                '或 None（表示没声明）'
            ),
            received=f'收到 {shown}，类型是 {kind}',
            next_action=(
                f'写成元组，如 ({shown},)；'
                '裸字符串会被逐字符当成参数名，于是一个名字都对不上、检查静默落空'
                if isinstance(declared, str)
                else "写成由参数名字符串组成的元组，如 ('command',)；"
                '声明 () 表示这个工具什么都不执行'
            ),
        )
    names = tuple(cast('Sequence[str]', declared))
    parameters = inspect.signature(tool.execute).parameters
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return names
    unknown = [name for name in names if name not in parameters]
    if unknown:
        raise ConfigError(
            expected=(
                f'{type(tool).__name__}.__tool_command_args__ 里的每个名字都是 execute 的参数名'
            ),
            received=f'{unknown} 不在 execute 的参数里，它收的是 {list(parameters)}',
            next_action=(
                f'把 __tool_command_args__ 改成 execute 真正的参数名（可选：{list(parameters)}），'
                '或改 execute 的签名；对不上的名字永远取不到值，那道危险判定的闸等于不存在'
            ),
        )
    return names


def _inspected_texts(tool: Tool, args: Mapping[str, Any]) -> tuple[list[str], bool]:
    """这次调用要检查的文本，以及这些文本是不是靠「没声明就全查」猜出来的。

    第二项决定结论里要不要带上那句怎么改的提示：**猜出来的那条路会对写文本的工具误报**，而修法
    是一行声明，把它写进结论就把一次莫名的阻断变成一次可自助修复的配置问题。
    """
    declared = _declared_command_args(tool)
    if declared is None:
        # 未声明的工具不为「读不出文本的值」报错：它本来就是「不知道哪些参数会被执行」，在它每一个
        # int 参数上抛异常会让整批工具用不了；换来的是这条路上的保证弱于声明过的那条，差别写在
        # Tool 的声明处。读不**完**仍然要响亮失败——那是规模问题，不是类型问题。
        guessed = [
            text for name, value in args.items() for text in _readable_texts(tool, name, value)
        ]
        return guessed, True
    texts: list[str] = []
    for name in declared:
        if name not in args:
            continue
        value = cast('object', args[name])
        if not _readable_as_text(value):
            raise ContractViolation(
                expected=(
                    f'{type(tool).__name__} 声明为可执行的参数 {name!r} 是文本、字节串，'
                    '或由它们组成的序列 / 映射'
                ),
                received=f'收到 {value!r}，类型是 {type(value).__name__}，读不出可检查的文本',
                next_action=(
                    f'把 {name!r} 传成命令文本或 argv 序列，'
                    f'或把它从 {type(tool).__name__}.__tool_command_args__ 里去掉；'
                    '读不出文本时每一条危险规则都对这次调用失效，而调用方以为有闸'
                ),
            )
        texts.extend(_readable_texts(tool, name, value))
    return texts, False


_UNDECLARED_NOTE = (
    '；这条结论来自猜测——{tool} 没有声明 __tool_command_args__，于是它的每个参数都按「会被执行」'
    '检查，一份讲命令的文本也会命中。若它其实什么都不执行，声明 __tool_command_args__ = ()；'
    '若只有部分参数会被执行，声明那几个参数名'
)


def _matched(rule: DangerRule, text: str) -> bool:
    try:
        return rule.matches(text)
    except Exception as exc:
        raise ContractViolation(
            expected=f'规则 {rule.rule_id} 的 matches 对任何一条命令文本都交出 True 或 False',
            received=f'它在这次调用上抛了 {type(exc).__name__}: {exc}',
            next_action=(
                f'修 {rule.rule_id} 的 matches；它收到的文本来自模型，可能为空、可能含不成对的'
                '引号、可能根本不是命令。一条会抛的规则同时让它之后声明的每一条规则都不再运行，'
                '整张规则表在这次调用上一起失效'
            ),
        ) from exc


def _validated_rules(rules: object) -> tuple[DangerRule, ...]:
    if isinstance(rules, str | bytes) or not isinstance(rules, Sequence):
        raise ConfigError(
            expected='rules 是 DangerRule 的序列，或 None 用默认表、() 关掉内核规则',
            received=f'收到 {type(rules).__name__}：{rules!r}',
            next_action=(
                '传一个 DangerRule 列表。str / bytes 也是 Sequence，逐字符迭代之后报出的错误'
                '会指着每一个字符而不是指着那次误传'
            ),
        )
    typed = cast('Sequence[object]', rules)
    validated: list[DangerRule] = []
    for index, rule in enumerate(typed):
        if not isinstance(rule, DangerRule):
            raise ConfigError(
                expected=f'rules[{index}] 是 DangerRule',
                received=f'收到 {type(rule).__name__}：{rule!r}',
                next_action='每一项都必须是 DangerRule；不要把字符串或其它对象塞进规则表',
            )
        if not callable(rule.matches):
            raise ConfigError(
                expected=f'rules[{index}].matches 可调用',
                received=f'收到 {type(rule.matches).__name__}',
                next_action='给这条规则一个 matches(text: str) -> bool 的纯函数',
            )
        raw_level = cast(object, rule.level)
        if not isinstance(raw_level, RiskLevel):
            raise ConfigError(
                expected=f'rules[{index}].level 是 RiskLevel 成员',
                received=f'收到 {raw_level!r}，类型是 {type(raw_level).__name__}',
                next_action=(
                    '写成 RiskLevel.HIGH / RiskLevel.LOW。RiskLevel 不混入 str，'
                    "'high' 既不 is 也不 == RiskLevel.HIGH：写错的规则会被当成没有风险放行，"
                    '强制升级也跳过它'
                ),
            )
        raw_id = cast(object, rule.rule_id)
        if not isinstance(raw_id, str) or not raw_id.strip() or raw_id != raw_id.strip():
            raise ConfigError(
                expected=f'rules[{index}].rule_id 是不带首尾空白的非空字符串',
                received=f'收到 {raw_id!r}，类型是 {type(raw_id).__name__}',
                next_action=(
                    '规则 ID 进日志、被宿主策略按字面匹配，写成 shell.privilege_escalation 这样的'
                    '字符串，不带首尾空白'
                ),
            )
        validated.append(rule)
    return tuple(validated)


class DangerJudge:
    """按规则表给一次调用下结论。无状态、无副作用——它抛出时调用方**知道**什么都没发生。"""

    def __init__(self, rules: object = None) -> None:
        # `None` 用默认规则表，`()` 是「我自己在别处做了闸」——两者不能合并，否则宿主没法关掉
        # 内核规则，而空表与默认表长得一样。入参拷一份：交进来的 list 此后被改也改不动结论。
        # `str` / `bytes` 单独挡：它们也是 Sequence，逐字符迭代之后报出的是一串指着字符的错误。
        self._rules: tuple[DangerRule, ...] = _validated_rules(
            DEFAULT_RULES if rules is None else rules
        )

    @property
    def rules(self) -> tuple[DangerRule, ...]:
        """按声明顺序的规则（结论取第一个命中的）：宿主把它记进回放文档。"""
        return self._rules

    def judge(self, tool: Tool, args: Mapping[str, Any]) -> DangerVerdict:
        """取第一个命中的规则。

        外层遍历规则、内层遍历参数：结论因此只取决于**规则的声明顺序**，与参数在 dict 里的位置
        无关。反过来写会让同一条命令换个参数名就换个规则 ID，而规则 ID 是要进日志被比对的。
        """
        texts, guessed = _inspected_texts(tool, args)
        for rule in self._rules:
            for text in texts:
                if _matched(rule, text):
                    reason = rule.reason
                    if guessed:
                        reason += _UNDECLARED_NOTE.format(tool=type(tool).__name__)
                    return DangerVerdict(rule_id=rule.rule_id, level=rule.level, reason=reason)
        return NO_RULE_MATCHED


@runtime_checkable
class DangerPolicy(Protocol):
    """把结论变成动作。宿主可以整个换掉它。

    **不是处置的入口**：`disposition_of` 先施加 spec §12.2 第 3 条的强制升级，之后才咨询它。
    """

    def decide(self, request: DangerRequest) -> Disposition: ...


class BlockHighRisk:
    """内核默认处置：高风险直接阻断，其余放行。

    默认阻断而不是询问：本计划还没有人工确认通道，而拒绝比等待有用——Agent 立刻拿到「这条路被
    规则挡住了」这个明确信息就能换一种做法；等一个不会有人来看的确认只是白耗一段超时。
    """

    def decide(self, request: DangerRequest) -> Disposition:
        return Disposition.BLOCK if request.verdict.level is RiskLevel.HIGH else Disposition.ALLOW


class AllowAll:
    """一律放行。给自己已经在别处做了闸的宿主（如整个跑在一次性容器里的批处理）。

    「一律」的范围是**被咨询到的那些**：经 `disposition_of` 时，溯源到不可信内容的高风险调用在
    它之前就被拦下，轮不到它表态。
    """

    def decide(self, request: DangerRequest) -> Disposition:
        return Disposition.ALLOW


def _validated_table(table: object) -> dict[str, Disposition]:
    if not isinstance(table, Mapping):
        raise ConfigError(
            expected='RuleTablePolicy 的 table 是「规则 ID → Disposition」的 Mapping',
            received=f'收到 {type(table).__name__} 类型的 {table!r}',
            next_action=(
                '传 dict 给 RuleTablePolicy，例如 '
                "RuleTablePolicy({'shell.privilege_escalation': Disposition.BLOCK})"
            ),
        )
    checked: dict[str, Disposition] = {}
    for key, value in cast('Mapping[object, object]', table).items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ConfigError(
                expected='RuleTablePolicy 表里的键是不带首尾空白的非空规则 ID 字符串',
                received=f'收到键 {key!r}，类型是 {type(key).__name__}',
                next_action=(
                    '把 RuleTablePolicy 的键写成 DangerVerdict.rule_id 的原样字符串，'
                    "例如 'shell.privilege_escalation'；它按字面逐字匹配"
                ),
            )
        if not isinstance(value, Disposition):
            raise ConfigError(
                expected=f'RuleTablePolicy 表里 {key!r} 的值是 Disposition 成员',
                received=f'收到 {value!r}，类型是 {type(value).__name__}',
                next_action=(
                    '给 RuleTablePolicy 传 Disposition.BLOCK / Disposition.CONFIRM / '
                    "Disposition.ALLOW；Disposition 不混入 str，'block' 这样的字符串不等于任何成员"
                ),
            )
        checked[key] = value
    return checked


class RuleTablePolicy:
    """按规则 ID 查表处置：表里有的照表；表外的高风险阻断，其余放行。

    表外的高风险按 BLOCK 而不是 ALLOW：规则表会随宿主组合的规则变长，一条宿主还没来得及登记的
    高风险规则不该被静默放行。表在构造时拷一份，交进来的 dict 此后被改，改不动已经建好的策略。
    """

    def __init__(self, table: object) -> None:
        self._table = _validated_table(table)

    @property
    def table(self) -> Mapping[str, Disposition]:
        """规则 ID → 处置，只读：宿主把它记进回放文档。"""
        return MappingProxyType(self._table)

    def decide(self, request: DangerRequest) -> Disposition:
        listed = self._table.get(request.verdict.rule_id)
        if listed is not None:
            return listed
        return Disposition.BLOCK if request.verdict.level is RiskLevel.HIGH else Disposition.ALLOW


def _received_instead(value: object) -> str:
    """`received` 那一栏；类对象单独点名，它与实例只差一对括号。"""
    if isinstance(value, type):
        return f'收到类对象 {value.__name__} 本身而不是它的实例——少了一对括号'
    return f'收到 {type(value).__name__} 类型的 {value!r}'


def validate_danger_options(judge: object, policy: object) -> None:
    """校验宿主交来的判定与处置；`None` 是「用内核默认」。收下这两者的每一处都要过它。

    参数标注为 `object`：要挡的正是违反标注的运行时取值。不校验的症状落在**第一次工具调用**上，
    而且指着工具——`policy` 少一对括号时 `decide` 少一个 `self` 实参、`policy` 根本没有 `decide`
    时是 `AttributeError`，两者都经中止路径变成一条说「本次调用中止于 …，是否已产生副作用未知」
    的 tool block，而错在声明它的那一行。

    `policy` 是类对象这一种要单独挡：`DangerPolicy` 是 `runtime_checkable` 的 Protocol，只查
    `decide` 属性在不在，而类对象上它也在，`isinstance` 因此对少一对括号完全无感。
    """
    if judge is not None and not isinstance(judge, DangerJudge):
        raise ConfigError(
            expected='danger_judge 是 DangerJudge 实例，或 None（用内核默认规则表）',
            received=_received_instead(judge),
            next_action=(
                'DangerJudge() 用默认规则表，DangerJudge(rules) 用自己的规则表，'
                'DangerJudge(()) 表示「我自己在别处做了闸」'
            ),
        )
    if policy is not None and (isinstance(policy, type) or not isinstance(policy, DangerPolicy)):
        raise ConfigError(
            expected=(
                'danger_policy 是有 decide(request) -> Disposition 方法的实例，'
                '或 None（用内核默认 BlockHighRisk）'
            ),
            received=_received_instead(policy),
            next_action=(
                '传 BlockHighRisk() / AllowAll()，或自己实现一个带 '
                'decide(self, request: DangerRequest) -> Disposition 的类并传它的实例'
            ),
        )


def validate_confirmation_handler(handler: object) -> None:
    """Validate the optional host confirmation transport before Runtime setup."""
    if handler is None:
        return
    if isinstance(handler, type):
        raise ConfigError(
            expected=(
                'confirmation_handler 是有 async confirm(runtime, request) 方法的实例，'
                '或 None（没有人工确认通道）'
            ),
            received=f'confirmation_handler.confirm 无法使用：{_received_instead(handler)}',
            next_action=(
                '传 MailboxConfirmationHandler()，或实现 ConfirmationHandler 并传实例；不要传类本身'
            ),
        )
    confirm = getattr(handler, 'confirm', None)
    if not callable(confirm):
        raise ConfigError(
            expected='confirmation_handler.confirm 是 async callable',
            received=(f'confirmation_handler.confirm={confirm!r}，类型是 {type(confirm).__name__}'),
            next_action=(
                '实现 async def confirm(self, runtime, request)，并返回 ConfirmationOutcome'
            ),
        )
    try:
        inspect.signature(confirm).bind(object(), object())
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            expected='confirmation_handler.confirm 能接收 runtime 与 request 两个位置参数',
            received=f'confirmation_handler.confirm 的签名不匹配：{exc}',
            next_action='改成 async def confirm(self, runtime, request) -> ConfirmationOutcome',
        ) from exc
    if not inspect.iscoroutinefunction(confirm):
        raise ConfigError(
            expected='confirmation_handler.confirm 是 async def',
            received=f'confirmation_handler.confirm={confirm!r} 是同步 callable',
            next_action='改成 async def confirm(self, runtime, request) -> ConfirmationOutcome',
        )


def _escalated(request: DangerRequest) -> bool:
    """这次处置是不是由强制升级给出（不咨询 policy）。`disposition_of` 与 trace 的 `decided_by`
    读同一个判据：各写一份，trace 就会在升级条件改动的那一天开始说谎。"""
    return request.verdict.level is RiskLevel.HIGH and request.provenance is TrustLevel.UNTRUSTED


def disposition_of(
    policy: DangerPolicy,
    request: DangerRequest,
    *,
    untrusted_high_risk: Disposition = Disposition.BLOCK,
) -> Disposition:
    """处置的唯一入口：先施加强制升级，再咨询宿主策略。

    **高风险 + 可溯源到不可信内容 → 使用宿主显式配置的升级处置，且不咨询 policy**
    （spec §12.2 第 3 条、§17 第 75 条）。默认 BLOCK；CONFIRM 与 ALLOW 只能由 Runtime Module 的
    `untrusted_high_risk` Flag 显式给出，不是模型授权。放在 policy 之后取严者会让这一条变成
    「策略之一」，不满足强制升级的结构约束。

    `provenance is None`（没有溯源过）**不**触发升级：这一条的前提是「可溯源到不可信内容」，前提
    没有被确认成立时冒充它成立，等于把一份没做过的检查报成做过了。溯源本身由调用方给出，于是
    `None` 与 `TRUSTED` 在这个出口上不可区分——把「没查过」与「查过且干净」区分开要在**出口之外**
    显式标记未验收，那需要一个现在还没有的生产者（D-28）。

    策略返回的东西当场校验：`Disposition` 刻意不混入 `str`，于是一个 `return 'block'` 的笔误既不
    `is` 也不 `==` 任何成员，下游那句 `is Disposition.BLOCK` 为假——症状是**放行**，正是最危险的
    那个方向。
    """
    raw_disposition = cast('object', untrusted_high_risk)
    if not isinstance(raw_disposition, Disposition):
        raise ContractViolation(
            expected='untrusted_high_risk 是 Disposition 成员',
            received=(f'收到 {raw_disposition!r}，类型是 {type(raw_disposition).__name__}'),
            next_action=(
                '传 Disposition.BLOCK / Disposition.CONFIRM / Disposition.ALLOW；'
                'Runtime 宿主请改 SafetyModule 的 untrusted_high_risk Flag'
            ),
        )
    if _escalated(request):
        return raw_disposition
    decided = cast('object', policy.decide(request))
    if not isinstance(decided, Disposition):
        raise ContractViolation(
            expected=f'{type(policy).__name__}.decide 交出一个 Disposition 成员',
            received=f'收到 {decided!r}，类型是 {type(decided).__name__}',
            next_action=(
                '把 decide 的 return 改成 Disposition.ALLOW / Disposition.BLOCK / '
                "Disposition.CONFIRM；Disposition 不混入 str，'block' 这类字符串与成员既不 is "
                '也不 ==，交回去会被下游读成「不是 BLOCK」而放行'
            ),
        )
    return decided
