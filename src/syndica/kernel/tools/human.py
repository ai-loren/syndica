"""Human-in-the-loop tools backed by the Runtime mailbox.

The tools publish a request on the in-process event stream, then wait for one
correlated mailbox reply. Terminal rendering and input remain surface concerns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from syndica import __version__
from syndica.kernel.agent import BaseAgent
from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import Event, EventBus
from syndica.kernel.ids import peek_key
from syndica.kernel.modules import FlagValue, RuntimeModule
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.waiting import UserInput, WaitResult, WaitState

if TYPE_CHECKING:
    from syndica.kernel.runtime import Runtime

__all__ = ['AskUser', 'AskUserQuestion', 'HumanInteractionModule']

_INPUT_REQUESTED = 'user_input_requested'
_QUESTION_REQUESTED = 'user_question_requested'
_MIN_OPTIONS = 2
_MAX_OPTIONS = 10


class _HumanRuntime(Protocol):
    events: EventBus

    async def wait_for(self, agent_id: str, waiter: UserInput) -> WaitResult: ...


def _question(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            expected='question 是非空白字符串',
            received=f'收到 question={value!r}，类型是 {type(value).__name__}',
            next_action='写出需要人回答的具体问题；空问题无法让人判断该输入什么',
        )
    return value


def _options(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise ConfigError(
            expected=f'options 是 {_MIN_OPTIONS}–{_MAX_OPTIONS} 个互不重复的非空白字符串',
            received=f'收到 options={value!r}，类型是 {type(value).__name__}',
            next_action='传字符串列表，例如 ["继续", "停止"]；不要传裸字符串',
        )
    choices = tuple(cast(Sequence[object], value))
    if not _MIN_OPTIONS <= len(choices) <= _MAX_OPTIONS:
        raise ConfigError(
            expected=f'options 含 {_MIN_OPTIONS}–{_MAX_OPTIONS} 项',
            received=f'收到 {len(choices)} 项：{choices!r}',
            next_action=f'保留 {_MIN_OPTIONS}–{_MAX_OPTIONS} 个明确选项',
        )
    invalid = [item for item in choices if not isinstance(item, str) or not item.strip()]
    if invalid:
        raise ConfigError(
            expected='options 的每一项都是非空白字符串',
            received=f'不合格项为 {invalid!r}',
            next_action='把每一项改成用户可以直接选择的非空白文本',
        )
    typed = cast(tuple[str, ...], choices)
    duplicates = [item for index, item in enumerate(typed) if item in typed[:index]]
    if duplicates:
        raise ConfigError(
            expected='options 互不重复',
            received=f'重复选项为 {duplicates!r}',
            next_action='删除重复项；同名选项无法表达不同决定',
        )
    return typed


def _unanswered(request_id: str, state: WaitState) -> ToolResult:
    words = {
        WaitState.TIMEOUT: '等待人类回答超时',
        WaitState.PREEMPTED: '这次提问被新的等待顶掉',
        WaitState.ABORTED: '等待被中止',
    }
    return ToolResult(
        output=(
            '期望: 收到一条与本次提问匹配的人类回答。'
            f' 实际: {words[state]}，没有回答内容。'
            ' 下一步: 不要把本条当成人类意见；根据当前上下文换一条可继续的路径。'
        ),
        meta={'request_id': request_id, 'state': state.value, 'answered': False},
    )


def _invalid_source(request_id: str, result: WaitResult) -> ToolResult:
    message = result.message
    sender = None if message is None else message.sender
    trust = None if message is None else message.trust.value
    return ToolResult(
        output=(
            "期望: 回答来自 sender='human' 且 trust='trusted'。"
            f' 实际: sender={sender!r} trust={trust!r}，内容已丢弃。'
            ' 下一步: 由可信的人机界面重新发起一轮提问；'
            'Agent 消息或不可信内容不能冒充人类答复。'
        ),
        meta={'request_id': request_id, 'state': 'invalid_source', 'answered': False},
    )


def _answered(request_id: str, result: WaitResult) -> ToolResult:
    message = result.message
    if message is None or message.sender != 'human' or message.trust is not TrustLevel.TRUSTED:
        return _invalid_source(request_id, result)
    return ToolResult(
        output=message.content,
        meta={'request_id': request_id, 'state': WaitState.ANSWERED.value, 'answered': True},
        trust=message.trust,
    )


class _HumanTool(Tool):
    """每个子类自己声明 `__tool_command_args__`，基类不替它声明（M4 D7）。"""

    def __init__(self, runtime: _HumanRuntime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def _ask(
        self,
        *,
        kind: str,
        question: object,
        timeout: object,
        options: tuple[str, ...] = (),
    ) -> ToolResult:
        prompt = _question(question)
        waiter = UserInput(timeout=cast(float, timeout))
        payload: dict[str, Any] = {'request_id': waiter.request_id, 'question': prompt}
        if options:
            payload['options'] = list(options)
        # 这次请求的稳定键（`wait_for` 随后用同一个键登记它）：`request_id` 每次都不同，回放按键
        # 给出人的答复（M3 D10）。字段名不能像密钥名：落盘前的逐叶脱敏会把它下面的 agent id 换掉。
        key = peek_key('request')
        if key is not None:
            payload['request_ref'] = list(key)
        await self._runtime.events.emit(Event(kind=kind, agent_id=self._agent_id, payload=payload))
        result = await self._runtime.wait_for(self._agent_id, waiter)
        # 了结事件让宿主撤下还挂着的提问：多个客户端开着同一个会话时，没答的那个不会再交一次答复，
        # 而没人等的答复会作为一条可信的人类消息进下一轮。
        await self._runtime.events.emit(
            Event(
                kind='human_request_resolved',
                agent_id=self._agent_id,
                payload={'request_id': waiter.request_id, 'outcome': result.state.value},
            )
        )
        if result.state is not WaitState.ANSWERED:
            return _unanswered(waiter.request_id, result.state)
        return _answered(waiter.request_id, result)


class AskUser(_HumanTool):
    """Ask a human for free-form input."""

    __tool_name__ = 'AskUser'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    async def execute(self, question: str, timeout: float = 300.0) -> ToolResult:
        """等待人类回答一个开放问题。
        question: 要向人展示的具体问题
        timeout: 最长等待秒数，必须是有限正数
        """
        return await self._ask(
            kind=_INPUT_REQUESTED,
            question=question,
            timeout=timeout,
        )


class AskUserQuestion(_HumanTool):
    """Ask a human to select one option."""

    __tool_name__ = 'AskUserQuestion'
    __tool_command_args__ = ()
    __tool_external_input__ = False
    __tool_schema__ = {
        'name': 'AskUserQuestion',
        'description': '等待人类从明确选项中选择一个。',
        'parameters': {
            'type': 'object',
            'properties': {
                'question': {
                    'type': 'string',
                    'description': '要向人展示的具体问题',
                },
                'options': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': '2 到 10 个互不重复的选项',
                },
                'timeout': {
                    'type': 'number',
                    'description': '最长等待秒数，必须是有限正数',
                    'default': 300.0,
                },
            },
            'required': ['question', 'options'],
        },
    }

    async def execute(
        self,
        question: str,
        options: Sequence[str],
        timeout: float = 300.0,
    ) -> ToolResult:
        """等待人类从明确选项中选择一个。
        question: 要向人展示的具体问题
        options: 2 到 10 个互不重复的选项
        timeout: 最长等待秒数，必须是有限正数
        """
        choices = _options(options)
        result = await self._ask(
            kind=_QUESTION_REQUESTED,
            question=question,
            timeout=timeout,
            options=choices,
        )
        if result.meta['state'] != WaitState.ANSWERED.value:
            return result
        answer = cast(str, result.output)
        if answer in choices:
            return result
        return ToolResult(
            output=(
                f'期望: 人类回答是选项之一 {list(choices)!r}。'
                f' 实际: 收到 {answer!r}，它不在选项中。'
                ' 下一步: 不要猜测人的意图；重新提问并让人选择一个列出的选项。'
            ),
            meta={
                'request_id': result.meta['request_id'],
                'state': 'invalid_answer',
                'answered': False,
                'options': list(choices),
            },
        )


def _human_tools(
    runtime: Runtime,
    agent: BaseAgent,
    flags: Mapping[str, FlagValue],
) -> Mapping[str, Tool]:
    del flags
    return {
        'AskUser': AskUser(runtime, agent.agent_id),
        'AskUserQuestion': AskUserQuestion(runtime, agent.agent_id),
    }


class HumanInteractionModule(RuntimeModule):
    """Install one pair of human-input tools per Agent."""

    def __init__(self) -> None:
        super().__init__(
            'human-interaction',
            tool_factories=(_human_tools,),
            version=__version__,
            description='给每个 Agent 装上向人提问的两个工具',
            capabilities=(),
            replay_safe=True,
        )
