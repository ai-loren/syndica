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
不是「已经确认安全」。明确不认的三类：经中间工具转手执行（`xargs ... sh -c`、`find -exec`）、把
命令拼成字符串再交给解释器（`sh -c "rm -rf x"`、`eval "rm -rf x"`），以及下载到文件后再单独执行
（`curl -o f; sh f`）。**同一形态换个平凡写法必须仍然命中**——多一段管道、多一次重定向、标志写在
路径后面、路径加一对引号、命令前面加一个绕 alias 的反斜杠（`\\rm`）都不算另一种形态。

`CONFIRM` 由宿主注入的 `ConfirmationHandler` 实现。默认没有 handler 时仍立即拒绝，不白耗等待；
`MailboxConfirmationHandler` 才把请求发进事件流，并经 `Runtime.wait_for` 收回一次性答复。确认使用
独立随机 request id，而不是模型给的 tool call id，迟到答复因此不能批准另一次调用。
"""

from __future__ import annotations

import fnmatch
import inspect
import os
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast, runtime_checkable

from syndica.kernel.context import ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import Event, EventBus
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
    永久兼容承诺。
    """

    rule_id: str
    level: RiskLevel
    reason: str
    matches: Callable[[str], bool]


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
    """

    request_id: str
    agent_id: str
    tool_call_id: str
    tool_name: str
    arguments: str
    verdict: DangerVerdict


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
        await runtime.events.emit(
            Event(
                kind='tool_confirmation_requested',
                agent_id=request.agent_id,
                payload={
                    'request_id': request.request_id,
                    'tool_call_id': request.tool_call_id,
                    'tool_name': request.tool_name,
                    'arguments': request.arguments,
                    'verdict': {
                        'rule_id': request.verdict.rule_id,
                        'level': request.verdict.level.value,
                        'reason': request.verdict.reason,
                    },
                },
            )
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
        'xvfb-run',
    }
)
_ESCALATORS = frozenset({'doas', 'pkexec', 'runuser', 'su', 'sudo'})
_DOWNLOADERS = frozenset({'aria2c', 'curl', 'fetch', 'wget'})
_INTERPRETERS = frozenset(
    {
        'ash',
        'bash',
        'csh',
        'dash',
        'fish',
        'ksh',
        'node',
        'perl',
        'php',
        'python',
        'python2',
        'python3',
        'ruby',
        'sh',
        'tcsh',
        'zsh',
    }
)
# 会把 stdin 当脚本执行的程序：heredoc 喂给它们时，正文就是命令。
_STDIN_RUNNERS = _INTERPRETERS | {'.', 'source'}
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
# 续行符在 shell 里被消费掉，`rm \<换行>-rf x` 是一条命令；不折叠它，`\n` 这个段边界会把标志切进
# 下一段，`rm` 那一段于是看不到 `-rf`。
_LINE_CONTINUATION = re.compile(r'\\\r?\n')


def _basename(token: str) -> str:
    # 前导 `\` 一并去掉：`\rm` 是绕开 `alias rm='rm -i'` 的标准写法，与 `/bin/rm`、`"rm"` 同族。
    return token.rsplit('/', 1)[-1].lstrip('\\')


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
    """(操作符后面那个词的去向, 操作符结束的位置)。"""
    if thorough and source.startswith('<<<', index):
        return _DISCARD, index + 3
    if thorough and source.startswith('<<-', index):
        return _HEREDOC_TABS, index + 3
    if thorough and source.startswith('<<', index):
        return _HEREDOC, index + 2
    return _TARGET, _end_of_redirection(source, index)


def _after_heredocs(
    source: str,
    start: int,
    heredocs: Sequence[tuple[str, bool, bool]],
    line: Sequence[tuple[bool, tuple[str, ...]]],
) -> int:
    """这一行登记的 heredoc 正文结束之后的位置；不该跳过时就是 start。

    只跳过写进文件的数据：结束标记带引号（正文不展开，里面的 `$(…)` 不执行），并且这一行既没有
    管道、也没有会把 stdin 当脚本执行的程序（`sh <<'EOF'` 执行的正是正文）。管道单独算：接在后面
    的程序不在解释器名单里（`| lua`）时，正文照样可能被执行。
    找不到结束标记也不跳：续行符折叠、写错的标记都会让它落空，而跳到末尾会把后面真正的命令一起
    藏起来。
    """
    if not heredocs or not all(quoted for _, quoted, _ in heredocs):
        return start
    for piped, tokens in line:
        if piped or any(
            _basename(tokens[index]) in _STDIN_RUNNERS for index in _program_candidates(tokens)
        ):
            return start
    position = start
    for delimiter, _, strip_tabs in heredocs:
        while True:
            if position >= len(source):
                return start
            newline = source.find('\n', position)
            stop = len(source) if newline == -1 else newline
            text = source[position:stop]
            position = stop + 1
            if (text.lstrip('\t') if strip_tabs else text).rstrip('\r') == delimiter:
                break
    return min(position, len(source))


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
    text: str, *, thorough: bool = False
) -> tuple[list[tuple[bool, tuple[str, ...]]], list[str]]:
    """(段, 整条命令里全部重定向的目标)。

    `thorough=False` 时段与 `_segments` 一字不差：默认规则的判定只取决于段。三族可选规则用
    `thorough=True`，多做四件会改变段的事，所以它们不进默认规则：
    - 空引号是一个词：`git -C '' push` 的 `''` 是 `-C` 的取值，丢掉它，`push` 就被当成取值吞掉；
    - 双引号里的 `$(…)` 与反引号照样执行，拆成新的一段；
    - `<<-` 是一个操作符；heredoc 的结束标记与 here-string 的词既不是 token 也不是目标；
    - 跳过「写进文件的数据」那种 heredoc 正文，见 `_after_heredocs`。

    目标不挂在段上：只有重定向、没有命令词的一段（`: ; > /etc/x` 的后半）不产出段，挂在段上它的
    目标就丢了。`2>&1` 的目标是 `1`——相对的词，按路径看落在根里。
    """
    segments: list[tuple[bool, tuple[str, ...]]] = []
    targets: list[str] = []
    tokens: list[str] = []
    token: list[str] = []
    heredocs: list[tuple[str, bool, bool]] = []
    closers: list[str] = []
    piped = False
    quote = ''
    quoted = False
    dropping = ''
    line_start = 0
    source = _LINE_CONTINUATION.sub('', text)
    index = 0

    def end_token() -> None:
        nonlocal dropping, quoted
        if token or (thorough and quoted):
            word = ''.join(token)
            if dropping == _TARGET:
                targets.append(word)
            elif dropping in (_HEREDOC, _HEREDOC_TABS):
                heredocs.append(
                    (word.replace('\\', ''), quoted or '\\' in word, dropping == _HEREDOC_TABS)
                )
            elif not dropping:
                tokens.append(word)
            dropping = ''
            token.clear()
        quoted = False

    def end_segment(next_piped: bool) -> None:
        nonlocal piped, dropping
        end_token()
        dropping = ''
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
        if quote:
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
        elif closers and char == closers[-1]:
            closers.pop()
            end_segment(False)
            quote = '"'
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
            end_segment(char == _PIPE)
            if thorough and char == '\n':
                index = _after_heredocs(source, index + 1, heredocs, segments[line_start:])
                heredocs.clear()
                line_start = len(segments)
                continue
        elif char.isspace():
            end_token()
        else:
            token.append(char)
        index += 1
    end_segment(False)
    return segments, targets


def _command_word_slots(tokens: Sequence[str]) -> list[tuple[int, str]]:
    """一段里处在**命令词位置**上的 (下标, 基名)。

    按位置取而不是「这段里出现过这个词」：后者让 `echo sudo apt install` 与 `git push --force`
    这类散文式参数每次都命中，而误报会被下游当噪声整条关掉。

    **wrapper 之后不再停在第一个非选项词上。** 哪些 wrapper 选项要吃掉下一个词无法从语法判断，
    而 `nice -n 19 rm` 的 `19` 是 `-n` 的取值、`timeout 30 rm` 的 `30` 是位置实参：停在它上面就
    把真正的命令词整个吞掉，`nice -n 19 rm -rf /` 于是在默认规则表下一条闸都没有。代价是 wrapper
    之后的普通实参也算候选命令词（`command -v sudo` 会误报成提权），这个方向的错还能换个写法，
    反方向的错是一句不说地放行。第一个命令词之前的保留字（`{`、`then`、`!`）跳过。
    """
    slots: list[tuple[int, str]] = []
    wrapped = False
    for index, token in enumerate(tokens):
        if (
            token.startswith('-')
            or _ASSIGNMENT.match(token)
            or (not slots and token in _RESERVED_WORDS)
        ):
            continue
        word = _basename(token)
        slots.append((index, word))
        if not wrapped and word not in _WRAPPERS:
            break
        wrapped = True
    return slots


def _command_words(tokens: Sequence[str]) -> list[str]:
    return [word for _, word in _command_word_slots(tokens)]


def _escalates_privilege(command: str) -> bool:
    """`sudo` / `doas` / `su` / `pkexec` / `runuser` 出现在命令词位置。

    `sudo -v`（刷新凭据）与 `sudo -l`（列权限）也算：它们不执行别的命令，判 HIGH 是刻意的——
    区分「这次 sudo 会不会真做事」要认全每个 sudo 选项，而认漏一个就是一道闸没了。
    """
    return any(
        word in _ESCALATORS for _, tokens in _segments(command) for word in _command_words(tokens)
    )


def _reads_its_script_from_stdin(tokens: Sequence[str]) -> bool:
    """这一段是不是「解释器把 stdin 当脚本执行」。

    带脚本实参的解释器执行的是那个文件，管道送进来的只是它的 stdin——`curl … | python3 x.py` 是
    每天都在跑的数据管道，判它远程脚本执行就是误报。选项与选项取值不算实参：`sh -s -- --flag`
    读的仍是 stdin。
    """
    for index, word in _command_word_slots(tokens):
        if word not in _INTERPRETERS:
            continue
        if not any(
            not later.startswith('-') and not _ASSIGNMENT.match(later)
            for later in tokens[index + 1 :]
        ):
            return True
    return False


def _pipes_a_download_into_an_interpreter(command: str) -> bool:
    """同一条管道链里，下载在前、解释器在后。

    链要按 `|` 断开重数：`curl x; sh y` 走的是文件、不是这条链，把 `;` 也算进来会让「下载过就算」
    扩散到整条命令行。中间隔多少段与判定无关——`| tee /tmp/a |` 挡不住后面那个 shell。
    """
    downloaded = False
    for piped, tokens in _segments(command):
        if not piped:
            downloaded = False
        if downloaded and _reads_its_script_from_stdin(tokens):
            return True
        if any(word in _DOWNLOADERS for word in _command_words(tokens)):
            downloaded = True
    return False


def _removes_recursively_and_forcibly(command: str) -> bool:
    """`rm` 的标志里同时出现递归与强制。

    标志扫整段而不只扫命令词后面那几个：`rm /tmp/x -rf` 是合法的 GNU 写法。长选项按前缀认
    （`--rec` 就是 `--recursive`），短选项按字母认且不分大小写（`-R` 即递归）——不逐个枚举拼法，
    枚举漏一种就是漏一道闸，而多认几种的代价只是一次误报。

    `--` 之后停止扫标志：那之后的每个词都是文件名，`rm -- -rf` 删的是一个叫 `-rf` 的文件。
    """
    for _, tokens in _segments(command):
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
    for _, tokens in _segments(command):
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
        matches=_pipes_a_download_into_an_interpreter,
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
    # 选项之后、程序词之前的位置实参个数（`timeout 60 cmd` 的 60）。
    positionals: int = 0
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
    while pending:
        index = pending.pop()
        launched = _launched_program(tokens, index)
        if launched is None:
            more = [
                later
                for later in range(index + 1, len(tokens))
                if not tokens[later].startswith('-') and not _ASSIGNMENT.match(tokens[later])
            ]
        else:
            more = [launched + offset for offset, _ in _command_word_slots(tokens[launched:])]
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
    if any(_names_git_dir(target) for target in targets):
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


@dataclass(frozen=True)
class _WorkspaceRoot:
    """构造规则时解析一次的工作区根。`matches` 只在这两个字符串上做字符串运算，不碰磁盘。

    同时留字面形态与解析掉符号链接之后的形态：macOS 上 `/tmp/x` 解析成 `/private/tmp/x`，命令里
    两种写法都会出现，只认一种就把另一种判成工作区外（越界）或判不出是根（删根）。
    """

    lexical: str
    real: str

    @classmethod
    def of(cls, root: object, factory: str) -> _WorkspaceRoot:
        shown, kind = repr(root), type(root).__name__
        text: object = None
        if isinstance(root, str | os.PathLike):
            text = os.fspath(cast('str | os.PathLike[str]', root))
        if not isinstance(text, str) or not text.startswith('/') or '\0' in text:
            raise ConfigError(
                expected=f'{factory} 的 root 是工作区根的绝对路径（str 或 os.PathLike）',
                received=f'收到 {shown}，类型是 {kind}',
                next_action=(
                    f'宿主先把工作区根解析成绝对路径再交给 {factory}（例如 Path(root).resolve()）；'
                    '规则不读 cwd，相对路径没有可以相对的地方'
                ),
            )
        lexical = posixpath.normpath(text)
        try:
            real = str(Path(lexical).resolve())
        except (OSError, RuntimeError) as exc:
            raise ConfigError(
                expected=f'{factory} 的 root 能解析掉符号链接',
                received=f'解析 {lexical!r} 时 {type(exc).__name__}: {exc}',
                next_action='检查工作区根路径上的符号链接（是否成环、是否有权限读），修好后再构造规则',
            ) from exc
        return cls(lexical=lexical, real=real)

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
        if any(self.points_outside(target) for target in targets):
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
        for _, tokens in _parse(command, thorough=True)[0]:
            for index in _program_candidates(tokens):
                if _basename(tokens[index]) == 'rm' and any(
                    self.covers_root(operand) for operand in _rm_operands(tokens[index + 1 :])
                ):
                    return True
        return False


def git_state_change_rule() -> DangerRule:
    """`shell.git_state_change`：这条命令会改 git 状态。

    命中：git 的子命令不在只读集合里（`git-push` 这类带连字符的可执行文件同样按子命令算）；
    `-c`、`--config-env`、`--exec-path`、`--git-dir`、`--work-tree`、`--bare` 与认不出的全局选项；
    `grep` 用 `-O` / `--open-files-in-pager` 执行命令；`GIT_` 开头的环境赋值（任何位置，`export`
    也算）；任何实参、`=` 之后的值、紧贴短选项的取值（`-o.git/x`）或重定向目标里有一段是 `.git`
    （不分大小写，含会展开出它的通配）。git 按命令词槽与启动器展开出来的程序找（`setsid git push`、
    `uv run --with rich git push`、`{ git push; }`、双引号里的 `$(git push)`）。

    已知误报：`find . -path ./.git -prune` 这类只读引用 `.git` 的命令；只读集合之外的只读用法
    （`git branch`、`git stash list`、`git remote -v`、`git config --get`）；`echo GIT_DIR=x`；
    wrapper 之后恰好写着 `git` 的普通实参；结束标记不带引号、喂进管道或交给解释器的 heredoc 正文。
    已知盲区：`sh -c "…"`、`python -c "…"`、`make`、脚本文件里的 git，以及引号内代码里嵌的
    `.git` 路径；`xargs` / `find -exec` 转手；变量（`$GIT push`）；反斜杠与 `$'…'` 转义
    （`.g\\it`）；花括号展开（`{.git,x}`）；名单之外的启动器与 wrapper（`xcrun`、`pdm run`、
    `eval`）。
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


def path_outside_workspace_rule(root: object) -> DangerRule:
    """`shell.path_outside_workspace`：实参或重定向目标指向工作区之外。

    `root` 是工作区根的绝对路径，构造时解析一次。命中：工作区外的绝对路径（`_PSEUDO_DEVICES`
    与 `/dev/fd/N` 除外）、`~` 开头、`$HOME` / `${HOME}` 开头、会逃出去的 `..`（`$PWD` 当 `.`）；
    `--选项=值` 按值判；不带目录的 `cd`、`cd -`、`pushd`、`popd`（`builtin cd`、`{ cd; }` 也算）。
    包含关系按规范化后的路径段比，兄弟目录 `<根>-other` 在外面。相对路径按 Bash 的 `cwd` 在根下
    任意一层都试一遍，接在解析掉符号链接的根上：从根看不越界的 `../../agents/proj/x`，cwd 在
    `src` 时就在外面。

    每段只豁免**确定的**程序词：第一个词，以及有选项表的 wrapper（`timeout 60`、`env -i`、
    `nice -n 5` 等）启动的程序，并且只在它是绝对路径时豁免；认不出就不再往后豁免。
    `timeout 60 cat /etc/passwd`、`setsid cat /etc/passwd`、`~/bin/x.sh`、`../other/run.sh` 因此
    都命中，`/usr/bin/python3 x.py` 不命中。

    已知盲区：`sh -c "…"`、`make`、脚本文件里的命令；变量与未加引号的命令替换（`$(pwd)`、
    `${HOME%/}`）；引号内代码里嵌的路径；选项与取值粘在一起的写法（`-o/etc/x`）；`dd of=` 这类
    不带 `--` 的 `名字=值`；工作区里指向外面的符号链接；以绝对路径给出的工作区外可执行文件
    （`/tmp/x.sh`）。已知误报：以 `/` 开头的 sed / awk 脚本与搜索模式（`sed -n '/def x/,/^$/p'`）、
    `/tmp` 下的临时文件、`echo $HOME`、`cd src && … && cd ..`（命令内的 `cd` 不跟踪）、结束标记
    不带引号的 heredoc 正文里的路径。
    """
    workspace = _WorkspaceRoot.of(root, 'path_outside_workspace_rule')
    return DangerRule(
        rule_id='shell.path_outside_workspace',
        level=RiskLevel.HIGH,
        reason=(
            '这次调用读写工作区之外的路径（绝对路径、~、$HOME、逃出根目录的 ..，'
            '或离开工作区的 cd），超出了这次任务的范围'
        ),
        matches=workspace.reaches_outside,
    )


def workspace_root_removal_rule(root: object) -> DangerRule:
    """`shell.workspace_root_removal`：`rm` 删掉工作区根本身、根下的全部内容，或根的祖先。

    `root` 同 `path_outside_workspace_rule`。命中的目标规范化之后是根（`.`、`./`、`.//`、
    `src/..`、`$PWD`、根的绝对路径、`../<根目录名>`）、根的祖先（`..`、`/`）、逐段通配能匹配到
    根或其祖先（`/srv/*/proj`、`../*`），或根下的「全部」（`*`、`./*`、`?*`、`[!.]*`）。相对目标
    按 Bash 的 `cwd` 在根下任意一层都试（cwd 在 `src` 时 `../../proj` 就是根），接在字面与解析掉
    符号链接的两种根上。不看 `-r` / `-f`：`rm *` 不带 `-r` 也删光根下所有文件。rm 按命令词槽与
    启动器展开出来的程序找。`rm -r build`、`rm *.pyc`、`rm -r ../build` 不命中。

    已知误报：`rm -r ?`（连 `a` 与 `z` 都匹配的通配按「全部」算）；cwd 就在根上时的
    `rm -r ../../<根目录名>`（删的是根外那个同名目录，照样按删根拦下）。已知盲区：`~` 与 `$PWD`
    以外的变量不展开；`$(pwd)` 与反引号；
    `find -delete`；`xargs rm`、`sh -c`、`make`；花括号展开（`{*,.*}`）；`git clean`（它归
    `shell.git_state_change`）。
    """
    workspace = _WorkspaceRoot.of(root, 'workspace_root_removal_rule')
    return DangerRule(
        rule_id='shell.workspace_root_removal',
        level=RiskLevel.HIGH,
        reason='这次调用删除工作区根目录本身或根下的全部内容，删掉的东西没有回收站',
        matches=workspace.removes_root,
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
    if request.verdict.level is RiskLevel.HIGH and request.provenance is TrustLevel.UNTRUSTED:
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
