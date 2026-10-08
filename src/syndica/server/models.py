"""HTTP / SSE / WebSocket 的数据形状。OpenAPI 快照由这些模型生成；响应在交出之前也按它们校验一遍，
文档与实际交出的东西因此不会分叉。由内核里 M12 才公开的类型（裁决、进度、预算视图）序列化来的部分在
快照里标 provisional。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    'AgentNode',
    'ArtifactDiff',
    'ArtifactList',
    'ArtifactOut',
    'ArtifactPreview',
    'ArtifactSummary',
    'ArtifactVersions',
    'FeedbackRequest',
    'FeedbackResult',
    'FlagList',
    'FlagOut',
    'RubricList',
    'RubricOut',
    'TopologyList',
    'TraceAnalysis',
    'TraceList',
    'TraceRow',
    'Block',
    'BudgetRequest',
    'CreateSession',
    'DoctorCheck',
    'DoctorReport',
    'ErrorBody',
    'EventEnvelope',
    'Health',
    'LoginRequest',
    'LoginResponse',
    'PendingRequest',
    'Roles',
    'RpcReply',
    'RpcRequest',
    'SessionList',
    'SessionSummary',
    'Snapshot',
    'ToolCallOut',
    'Transcript',
]


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ErrorBody(_Model):
    expected: str
    received: str
    next_action: str


class Health(_Model):
    nonce: str
    version: str


class LoginRequest(_Model):
    code: str


class LoginResponse(_Model):
    token: str


class BudgetRequest(_Model):
    # strict：宽松模式把 true 收成 1、"3" 收成 3、1.0 收成 1，转换在内核的 Budget 看到值之前就做完，
    # 内核那道「拒 bool」的检查于是从不触发——`{"turns": true}` 静默变成 1 轮的预算。
    model_config = ConfigDict(extra='forbid', strict=True)

    tokens: int | None = Field(default=None, ge=1)
    cost: str | None = Field(default=None, description='十进制金额字符串，例如 "0.50"')
    wall_clock_s: float | None = Field(default=None, gt=0)
    turns: int | None = Field(default=None, ge=1)


class CreateSession(_Model):
    role: str = 'Orchestrator'
    budget: BudgetRequest | None = None


class SessionSummary(_Model):
    sid: str
    agent: str
    status: str
    turns: int
    created_at: float
    updated_at: float
    live: bool


class SessionList(_Model):
    sessions: list[SessionSummary]


class AgentNode(_Model):
    agent_id: str
    name: str
    parent_id: str | None
    status: str


class ToolCallOut(_Model):
    id: str
    name: str
    arguments: str


class ArtifactOut(_Model):
    id: str
    path: str
    kind: str
    mime: str
    version: str
    producer: str
    is_deliverable: bool


class ArtifactSummary(ArtifactOut):
    versions: int
    note: str = Field(
        default='',
        description=(
            '最近一次 Deliver 时模型写的说明（不可信的自由文本，按文本显示）；'
            '这个 server 进程里没交付过就是空串'
        ),
    )


class ArtifactList(_Model):
    artifacts: list[ArtifactSummary]


class ArtifactVersions(_Model):
    versions: list[ArtifactOut]


class ArtifactPreview(_Model):
    text: str


class ArtifactDiff(_Model):
    summary: str
    changed: bool
    unified: str


class FeedbackRequest(_Model):
    id: str
    version: str
    text: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class RubricOut(_Model):
    id: str
    statement: str
    kind: str
    status: str
    origin: str


class FeedbackResult(_Model):
    rubric: RubricOut | None


class RubricList(_Model):
    goal: str | None
    rubrics: list[RubricOut]


class TraceRow(_Model):
    trace_id: str
    started_ns: int
    ended_ns: int | None
    status: str | None


class TraceList(_Model):
    traces: list[TraceRow]


class TraceAnalysis(_Model):
    trace_id: str
    status: str | None
    started_ns: int
    ended_ns: int | None
    agents: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    spawns: list[dict[str, Any]]
    llm_calls: int
    tokens: int


class FlagOut(_Model):
    key: str
    value: Any
    default: Any
    description: str
    choices: list[Any]


class FlagList(_Model):
    flags: list[FlagOut]


class TopologyList(_Model):
    topologies: list[dict[str, Any]]


class Block(_Model):
    role: str
    text: str
    trust: Literal['trusted', 'untrusted']
    kind: str | None
    tool_calls: list[ToolCallOut]
    tool_call_id: str | None
    artifacts: list[ArtifactOut]
    tool_meta: dict[str, Any]


class Transcript(_Model):
    generation: int
    blocks: list[Block]


class PendingRequest(_Model):
    model_config = ConfigDict(extra='allow')

    request_id: str
    kind: Literal['confirmation', 'user_input', 'user_question', 'budget']
    agent_id: str


class Snapshot(_Model):
    sid: str
    root_id: str
    as_of: str
    agents: list[AgentNode]
    transcripts: dict[str, Transcript]
    budget: dict[str, Any] | None = Field(json_schema_extra={'x-syndica-stability': 'provisional'})
    progress: dict[str, Any] | None = Field(
        json_schema_extra={'x-syndica-stability': 'provisional'}
    )
    pending: list[PendingRequest]


class DoctorCheck(_Model):
    name: str
    ok: bool
    expected: str | None = None
    received: str | None = None
    next_action: str | None = None


class DoctorReport(_Model):
    ok: bool
    checks: list[DoctorCheck]


class Roles(_Model):
    roles: list[str]


class EventEnvelope(_Model):
    """SSE 一条事件的 data：`event:` 行是种类，`id:` 行是 `<epoch>:<seq>`。"""

    agent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict[str, Any])
    time_ns: int = 0
    sid: str | None = None
    seq: int | None = None


class RpcRequest(_Model):
    model_config = ConfigDict(extra='allow')

    op: Literal['auth', 'send', 'answer', 'abort', 'grant', 'budget_answer', 'activate_rubric']
    id: str | int | None = None


class RpcReply(_Model):
    id: str | int | None
    ok: bool
    error: ErrorBody | None = None
    result: dict[str, Any] | None = None
