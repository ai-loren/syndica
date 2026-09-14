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

**本计划只落地 `ALLOW` 与 `BLOCK` 两档处置的实现。** `CONFIRM` 这个取值现在就存在，因为并发分段
要按「这次调用需不需要人工授权」判不安全（spec §5.6），没有这个取值那条从严规则就无从表达；真正
的人工确认交互随 CLI 落在计划 6。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, cast, runtime_checkable

from syndica.kernel.context import ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.tool import Tool

__all__ = [
    'DEFAULT_RULES',
    'NO_RULE_MATCHED',
    'AllowAll',
    'BlockHighRisk',
    'DangerJudge',
    'DangerPolicy',
    'DangerRequest',
    'DangerRule',
    'DangerVerdict',
    'Disposition',
    'RiskLevel',
    'disposition_of',
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


# 命令词之前可以出现的环境变量赋值（`FOO=1 rm -rf x`）。
_ASSIGNMENT = re.compile(r'\A[A-Za-z_][A-Za-z0-9_]*=')
# 这些命令后面跟的仍然是一条命令，因此命令词要继续往后找。
_WRAPPERS = frozenset(
    {
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


def _segments(text: str) -> list[tuple[bool, tuple[str, ...]]]:
    """把一条命令拆成若干段，每段是 (是否由管道接在上一段之后, 去掉引号的 token)。

    重定向（`2>/dev/null`、`</dev/null`、`2>&1`、`&>log`）既不开新段也不留 token：fd 前缀与操作符
    属于语法，目标是一个路径。**目标必须丢掉而不是留作 token**——留着它，`curl x | sh > /tmp/out`
    的第二段就多出一个非选项词，「解释器带脚本实参」那条判据会把它读成「执行的是本地脚本」。

    引号不成对、反引号不闭合都不报错：这条文本来自模型，判定在它上面抛异常等于让一次畸形输入
    变成整轮失败。拆不干净的部分按原样留在 token 里，规则至多多认几种。
    """
    segments: list[tuple[bool, tuple[str, ...]]] = []
    tokens: list[str] = []
    token: list[str] = []
    piped = False
    quote = ''
    dropping = False
    source = _LINE_CONTINUATION.sub('', text)
    index = 0

    def end_token() -> None:
        nonlocal dropping
        if token:
            if not dropping:
                tokens.append(''.join(token))
            dropping = False
            token.clear()

    def end_segment(next_piped: bool) -> None:
        nonlocal piped, dropping
        end_token()
        dropping = False
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
            else:
                token.append(char)
        elif char in _QUOTES:
            quote = char
        elif char in _REDIRECTS or (char == '&' and source[index + 1 : index + 2] == '>'):
            if char in _REDIRECTS and ''.join(token).isdigit():
                # `2>` 的 `2` 是 fd 编号、是操作符的一部分；`hi2>x` 的 `hi2` 才是一个词。
                token.clear()
            end_token()
            dropping = True
            index = _end_of_redirection(source, index)
            continue
        elif char in _BOUNDARIES:
            end_segment(char == _PIPE)
        elif char.isspace():
            end_token()
        else:
            token.append(char)
        index += 1
    end_segment(False)
    return segments


def _command_word_slots(tokens: Sequence[str]) -> list[tuple[int, str]]:
    """一段里处在**命令词位置**上的 (下标, 基名)。

    按位置取而不是「这段里出现过这个词」：后者让 `echo sudo apt install` 与 `git push --force`
    这类散文式参数每次都命中，而误报会被下游当噪声整条关掉。

    **wrapper 之后不再停在第一个非选项词上。** 哪些 wrapper 选项要吃掉下一个词无法从语法判断，
    而 `nice -n 19 rm` 的 `19` 是 `-n` 的取值、`timeout 30 rm` 的 `30` 是位置实参：停在它上面就
    把真正的命令词整个吞掉，`nice -n 19 rm -rf /` 于是在默认规则表下一条闸都没有。代价是 wrapper
    之后的普通实参也算候选命令词（`command -v sudo` 会误报成提权），这个方向的错还能换个写法，
    反方向的错是一句不说地放行。
    """
    slots: list[tuple[int, str]] = []
    wrapped = False
    for index, token in enumerate(tokens):
        if token.startswith('-') or _ASSIGNMENT.match(token):
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


class DangerJudge:
    """按规则表给一次调用下结论。无状态、无副作用。"""

    def __init__(self, rules: Sequence[DangerRule] | None = None) -> None:
        # `None` 用默认规则表，`()` 是「我自己在别处做了闸」——两者不能合并，否则宿主没法关掉
        # 内核规则，而空表与默认表长得一样。入参拷一份：交进来的 list 此后被改也改不动结论。
        self._rules: tuple[DangerRule, ...] = tuple(DEFAULT_RULES if rules is None else rules)

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


def disposition_of(policy: DangerPolicy, request: DangerRequest) -> Disposition:
    """处置的唯一入口：先施加强制升级，再咨询宿主策略。

    **高风险 + 可溯源到不可信内容 → 阻断，且不咨询 policy**（spec §12.2 第 3 条、§17 第 75 条）。
    放在 policy 之后取严者也能得到同样的答案，但那样这一条就成了「策略之一」；不允许模型自授权
    要求它是结构上关不掉的一步。

    `provenance is None`（没有溯源过）**不**触发升级：这一条的前提是「可溯源到不可信内容」，前提
    没有被确认成立时冒充它成立，等于把一份没做过的检查报成做过了。溯源本身由调用方给出，于是
    `None` 与 `TRUSTED` 在这个出口上不可区分——把「没查过」与「查过且干净」区分开要在**出口之外**
    显式标记未验收，那需要一个现在还没有的生产者（D-28）。

    策略返回的东西当场校验：`Disposition` 刻意不混入 `str`，于是一个 `return 'block'` 的笔误既不
    `is` 也不 `==` 任何成员，下游那句 `is Disposition.BLOCK` 为假——症状是**放行**，正是最危险的
    那个方向。
    """
    if request.verdict.level is RiskLevel.HIGH and request.provenance is TrustLevel.UNTRUSTED:
        return Disposition.BLOCK
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
