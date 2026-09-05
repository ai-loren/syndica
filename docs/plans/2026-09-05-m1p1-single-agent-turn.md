# 里程碑 1 · 计划 1：单 Agent 一轮闭环

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 能用 Python 创建一个 Agent，给它一句话，它调用 LLM、执行工具、返回结果——内核最小可运行闭环。

**Architecture:** 按 spec §4 的进程模型建骨架：可序列化状态放 `AgentRegistry`（Protocol + 内存实现），运行时对象放进程内 `LocalAgentTable`。`Runtime` 负责生命周期，`BaseAgent` 只跑"LLM + 工具"循环。LLM 与工具都经抽象层接入，本计划用脚本化假 LLM 驱动，真 provider 留到计划 4。

**Tech Stack:** Python 3.11+、pydantic、pytest + pytest-asyncio、uv、ruff、pyright

## Global Constraints

以下逐条摘自 [spec](../design/2026-09-03-architecture.md)，**每个任务都隐含包含本节要求**：

- Python **3.11** 起。
- **kernel 零重依赖**：只允许标准库 + `pydantic` + 一个 HTTP 客户端。厂商 SDK 一律进 backend。
- **kernel 不读环境变量**。路径、凭证、开关一律由宿主通过构造参数注入。
- **kernel 不依赖任何 surface 或 backend**；backend 之间互不依赖。
- **公开 API 全量类型标注**，随包发布 `py.typed`。
- **错误消息三件套**：期望什么 / 实际收到什么 / 下一步做什么。缺一不可。
- **注释只写代码表达不了的硬约束**，不写变更历史、不写"我为什么没那么做"。
- 公开 API 的范围包括：函数签名、**切点名、Store Protocol、Flag key、声明式 Agent 清单字段**。
- **dataclass 的容器默认值用参数化泛型别名作工厂**（`field(default_factory=list[ToolCall])`），不用裸 `list` / `dict` —— 裸形式在 `src` 的 pyright strict 下会被判成 `list[Unknown]` 而报错。

---

## 文件结构

```
src/syndica/kernel/
    __init__.py
    errors.py             异常类型（含三件套错误消息的基类）
    context.py            TrustLevel / ArtifactRef / ToolCall / ContextBlock / Context / StopReason
    agent_types.py        AgentStatus / ExitMode / AgentInfo
    message.py            Trigger / Message / DeliveryResult
    protocols/
        __init__.py
        registry.py       AgentRegistry Protocol
        broker.py         MessageBroker Protocol
    stores/
        __init__.py
        memory_registry.py    InMemoryAgentRegistry
        memory_broker.py      InMemoryMessageBroker
    contracts/            契约测试套件，随包发布（spec §13.3）
        __init__.py
        registry.py       AgentRegistryContract
        broker.py         MessageBrokerContract
    local_table.py        LocalAgentTable
    events.py             EventBus（进程内可订阅，spec §9.1 末条）
    hooks/
        __init__.py
        junctions.py      Junction 枚举 + INTERCEPTABLE / OBSERVABLE
        result.py         HookResult
        center.py         HooksCenter
    llm/
        __init__.py
        base.py           LLMConfig / LLM Protocol / LLMResponse
        scripted.py       ScriptedLLM（测试用；spec §13.1 的基础）
    tool.py               ToolResult / Tool 基类 / schema 生成
    tool_executor.py      ToolExecutor（本计划仅串行）
    agent.py              BaseAgent（turn 循环）
    runtime.py            Runtime（spawn / wait / send 最小闭环）

tests/
    contracts/test_memory_registry.py
    contracts/test_memory_broker.py
    kernel/test_context.py
    kernel/test_hooks.py
    kernel/test_tool.py
    kernel/test_tool_executor.py
    kernel/test_agent_turn.py
    kernel/test_runtime.py
    integration/test_single_agent_turn.py

scripts/check_deps.py     依赖方向校验（Global Constraints 第 4 条必须机器可验）
pyproject.toml
```

**责任划分依据**：状态类型（`context` / `agent_types` / `message`）与行为（`runtime` / `agent`）分开，因为前者被所有层引用而后者只被上层引用。`protocols` 与 `stores` 分开，因为 Protocol 是公开契约而实现可替换。`contracts` 单独成包**并随发行版发布**——第三方写 backend 必须能 import 它。

---

## Task 1: 项目脚手架与依赖方向校验

**Files:**
- Create: `pyproject.toml`
- Create: `src/syndica/__init__.py`
- Create: `src/syndica/kernel/__init__.py`
- Create: `src/syndica/py.typed`
- Create: `scripts/check_deps.py`
- Test: `tests/test_deps.py`

**Interfaces:**
- Consumes: 无
- Produces: 可安装的包 `syndica`；`uv run python scripts/check_deps.py` 返回 0 表示依赖方向合规

- [ ] **Step 1: 写失败测试**

```python
# tests/test_deps.py
import subprocess
import sys


def test_dependency_direction_check_passes():
    """kernel 不得 import surface 或 backend（Global Constraints 第 4 条）。"""
    result = subprocess.run(
        [sys.executable, 'scripts/check_deps.py'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dependency_direction_check_catches_violation(tmp_path, monkeypatch):
    """校验脚本自身必须能抓到违规，否则它只是装饰。"""
    bad = tmp_path / 'src' / 'syndica' / 'kernel' / 'bad.py'
    bad.parent.mkdir(parents=True)
    bad.write_text('from syndica.cli import main\n')
    result = subprocess.run(
        [sys.executable, 'scripts/check_deps.py', '--root', str(tmp_path / 'src')],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert 'kernel/bad.py' in result.stdout
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_deps.py -v`
Expected: FAIL —— `scripts/check_deps.py` 不存在，returncode 为 2

- [ ] **Step 3: 写 pyproject.toml**

```toml
[project]
name = "syndica"
version = "0.0.0"
description = "A multi-agent harness for verifiable knowledge work"
readme = "README.md"
requires-python = ">=3.11"
license = { file = "LICENSE" }
authors = [{ name = "Loren Zhang" }]
dependencies = ["pydantic>=2.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "ruff>=0.6", "pyright>=1.1"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/syndica"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.ruff.format]
quote-style = "single"

[tool.pyright]
include = ["src", "tests"]
pythonVersion = "3.11"
typeCheckingMode = "strict"
```

- [ ] **Step 4: 写包入口与 py.typed**

```python
# src/syndica/__init__.py
"""Syndica — a multi-agent harness for verifiable knowledge work."""

__all__ = ['__version__']

__version__ = '0.0.0'
```

```python
# src/syndica/kernel/__init__.py
"""Kernel: runtime, agent, tools, hooks, protocols.

零重依赖：只允许标准库 + pydantic + 一个 HTTP 客户端。
不读环境变量：路径 / 凭证 / 开关由宿主注入。
"""
```

`src/syndica/py.typed` 为空文件。

- [ ] **Step 5: 写依赖方向校验脚本**

```python
# scripts/check_deps.py
"""校验 spec §3.2 的依赖方向规则。

kernel 不得 import surface（cli / server / runner）或 backend。
surface 之间互不依赖。backend 之间互不依赖。
"""

from __future__ import annotations

import argparse
import ast
import sys

from pathlib import Path

SURFACES = frozenset({'cli', 'server', 'runner'})
FORBIDDEN_FROM_KERNEL = SURFACES | {'backends'}


def _imported_syndica_subpackages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split('.')
        elif isinstance(node, ast.Import):
            parts = []
            for alias in node.names:
                parts = alias.name.split('.')
                if len(parts) >= 2 and parts[0] == 'syndica':
                    found.add(parts[1])
            continue
        else:
            continue
        if len(parts) >= 2 and parts[0] == 'syndica':
            found.add(parts[1])
    return found


def check(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted((root / 'syndica' / 'kernel').rglob('*.py')):
        for pkg in _imported_syndica_subpackages(path):
            if pkg in FORBIDDEN_FROM_KERNEL:
                rel = path.relative_to(root / 'syndica')
                violations.append(
                    f'{rel}: kernel imports {pkg!r}. '
                    f'kernel 不得依赖 surface 或 backend（spec §3.2 规则 1）。'
                    f' 改为在 kernel 定义 Protocol，由 {pkg} 提供实现并由宿主注入。'
                )
    for surface in sorted(SURFACES):
        base = root / 'syndica' / surface
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            for pkg in _imported_syndica_subpackages(path):
                if pkg in SURFACES and pkg != surface:
                    rel = path.relative_to(root / 'syndica')
                    violations.append(
                        f'{rel}: surface {surface!r} imports surface {pkg!r}. '
                        f'surface 之间必须互不依赖（spec §3.2 规则 2）。'
                        f' 把共用逻辑下沉到 kernel。'
                    )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='src', help='源码根目录，默认 src')
    args = parser.parse_args()
    violations = check(Path(args.root))
    for line in violations:
        print(line)
    if violations:
        print(f'\n共 {len(violations)} 处违规。')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv sync --extra dev && uv run pytest tests/test_deps.py -v`
Expected: 2 passed

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml src/syndica scripts/check_deps.py tests/test_deps.py
git commit -m "feat(kernel): 项目脚手架与依赖方向校验

依赖方向不能靠纪律维持，第一个 PR 就可能把 backend import 进 kernel，
所以 spec §3.2 的规则从第一天就是可执行检查。校验脚本自带反向测试——
它必须能抓到违规，否则只是装饰。"
```

---

## Task 2: 错误基类（错误消息三件套）

**Files:**
- Create: `src/syndica/kernel/errors.py`
- Test: `tests/kernel/test_errors.py`

**Interfaces:**
- Consumes: 无
- Produces: `SyndicaError(expected: str, received: str, next_action: str)`；`.message` 属性返回拼装后的完整消息。子类 `ContractViolation`、`ConfigError`、`LLMResponseError`、`ToolFatalError`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_errors.py
import pytest

from syndica.kernel.errors import SyndicaError


def test_message_contains_all_three_parts():
    err = SyndicaError(
        expected='kind=spreadsheet 的处理器唯一',
        received='syndica-office 与 syndica-sheets 都注册了 mime=xlsx',
        next_action='卸载其中之一，或用更具体的 mime 区分',
    )
    text = str(err)
    assert 'kind=spreadsheet 的处理器唯一' in text
    assert 'syndica-office' in text
    assert '卸载其中之一' in text


def test_all_three_parts_are_required():
    """三件套缺一不可（Global Constraints）——构造时就拦，不留到 code review。"""
    with pytest.raises(ValueError, match='next_action'):
        SyndicaError(expected='a', received='b', next_action='')


def test_tool_fatal_error_also_carries_the_contract():
    """致命工具错误会传播到 agent loop，因此它同样要说清下一步该做什么。"""
    from syndica.kernel.errors import ToolFatalError

    err = ToolFatalError(
        expected='目标仓库存在且可写',
        received='路径不存在，且父目录不可写',
        next_action='换一个可写路径，或先创建父目录',
    )
    assert isinstance(err, SyndicaError)
    assert '换一个可写路径' in str(err)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_errors.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.errors'`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/errors.py
"""异常基类。

面向调用方的错误必须让对方在不读源码的前提下自行修复，因此三件套在构造时强制。
"""

from __future__ import annotations

__all__ = [
    'ConfigError',
    'ContractViolation',
    'LLMResponseError',
    'SyndicaError',
    'ToolFatalError',
]


class SyndicaError(Exception):
    """携带「期望 / 实际 / 下一步」三件套的基类。"""

    def __init__(self, *, expected: str, received: str, next_action: str) -> None:
        for name, value in (
            ('expected', expected),
            ('received', received),
            ('next_action', next_action),
        ):
            if not value.strip():
                raise ValueError(
                    f'SyndicaError 需要非空的 {name}。'
                    f' 收到: {name}={value!r}。'
                    f' 三件套缺一不可，补齐后重新构造。'
                )
        self.expected = expected
        self.received = received
        self.next_action = next_action
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return f'期望: {self.expected}\n实际: {self.received}\n下一步: {self.next_action}'


class ContractViolation(SyndicaError):
    """Protocol 契约被违反。"""


class ConfigError(SyndicaError):
    """配置或声明不合法。"""


class LLMResponseError(SyndicaError):
    """LLM 响应无法作为有效结果使用。"""


class ToolFatalError(SyndicaError):
    """工具遇到重试也不可能成功的错误。

    继承本类的异常会**向上传播到 agent loop**，其余异常由 ToolExecutor 转成
    给模型看的 output（spec §10）。「什么算致命」由工具作者决定——ToolExecutor
    没有信息做这个判断。

    致命：目标资源不存在且无法创建、权限不足、依赖的外部服务已下线。
    非致命：参数不合法、临时超时、目标当前被占用——模型换个方式或换个参数可能成功。
    """
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_errors.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/errors.py tests/kernel/test_errors.py
git commit -m "feat(kernel): 错误基类，三件套在构造时强制

开源项目的错误消息质量直接决定 issue 数量。把「期望/实际/下一步」做成
构造参数而不是文档约定，是唯一能真正落地的办法。"
```

---

## Task 3: 上下文核心类型

**Files:**
- Create: `src/syndica/kernel/context.py`
- Test: `tests/kernel/test_context.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `TrustLevel`（`TRUSTED` / `UNTRUSTED`）
  - `ArtifactRef(id, path, kind, mime, version, producer, is_deliverable)` frozen
  - `ToolCall(id, name, arguments)`
  - `ContextBlock(role, content, tool_calls, tool_call_id, artifacts, trust, extra)`
  - `StopReason`（`COMPLETED` / `ABORTED` / `ERROR` / `MAX_TURNS`）
  - `Context.append(block)` / `.current_traj` / `.stop_reason`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_context.py
from syndica.kernel.context import (
    ArtifactRef,
    Context,
    ContextBlock,
    StopReason,
    ToolCall,
    TrustLevel,
)


def test_append_preserves_order():
    ctx = Context()
    ctx.append(ContextBlock(role='user', content='hi'))
    ctx.append(ContextBlock(role='assistant', content='hello'))
    assert [b.role for b in ctx.current_traj] == ['user', 'assistant']


def test_block_defaults_to_trusted():
    assert ContextBlock(role='user', content='hi').trust is TrustLevel.TRUSTED


def test_untrusted_marking_is_explicit():
    block = ContextBlock(role='tool', content='<html>', trust=TrustLevel.UNTRUSTED)
    assert block.trust is TrustLevel.UNTRUSTED


def test_block_carries_artifact_refs():
    ref = ArtifactRef(
        id='a1',
        path='report.md',
        kind='document',
        mime='text/markdown',
        version='v1',
        producer='agent-1',
        is_deliverable=True,
    )
    block = ContextBlock(role='tool', content='written', artifacts=[ref])
    assert block.artifacts[0].id == 'a1'


def test_tool_call_keeps_raw_arguments():
    """参数解析失败也必须留下原文，否则无法诊断模型输出了什么。"""
    call = ToolCall(id='c1', name='Read', arguments='{not json')
    assert call.arguments == '{not json'


def test_stop_reason_starts_unset():
    ctx = Context()
    assert ctx.stop_reason is None
    ctx.stop_reason = StopReason.COMPLETED
    assert ctx.stop_reason is StopReason.COMPLETED
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_context.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.context'`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/context.py
"""上下文数据类型。

Context 只承载产出与终态：轨迹块，以及一个终止原因（含 ABORTED）。
**运行中的控制状态不在这里** —— 当前状态、是否被挂起、中止是否已置位都归
Registry（spec §4.3）。理由是 Agent 阻塞或崩溃时无法自报状态，所以状态必须
在它之外可读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

__all__ = [
    'ArtifactRef',
    'Context',
    'ContextBlock',
    'Role',
    'StopReason',
    'ToolCall',
    'TrustLevel',
]

Role = Literal['system', 'user', 'assistant', 'tool']


class TrustLevel(Enum):
    """内容可信度。UNTRUSTED 必须随衍生内容传递（spec §12.2 第 2 条）。"""

    TRUSTED = 'trusted'
    UNTRUSTED = 'untrusted'


class StopReason(Enum):
    COMPLETED = 'completed'
    ABORTED = 'aborted'
    ERROR = 'error'
    MAX_TURNS = 'max_turns'


@dataclass(frozen=True)
class ArtifactRef:
    """产物引用。version 是不透明字符串，含义由隔离策略决定（spec §7.2）。"""

    id: str
    path: str
    kind: str
    mime: str
    version: str
    producer: str
    is_deliverable: bool = False


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ContextBlock:
    role: Role
    content: str | list[Any] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list[ToolCall])
    tool_call_id: str | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list[ArtifactRef])
    trust: TrustLevel = TrustLevel.TRUSTED
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


class Context:
    def __init__(self) -> None:
        self._blocks: list[ContextBlock] = []
        self.stop_reason: StopReason | None = None

    @property
    def current_traj(self) -> list[ContextBlock]:
        return list(self._blocks)

    def append(self, block: ContextBlock) -> None:
        self._blocks.append(block)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_context.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/context.py tests/kernel/test_context.py
git commit -m "feat(kernel): 上下文核心类型

artifacts 与 trust 两个字段现在就放进 ContextBlock，尽管它们的行为分别在
里程碑 7 与 5 才实现。理由是字段位置改起来要动所有遍历 context 的代码，
而加两个带默认值的字段成本为零。

ToolCall.arguments 存原始文本而非解析后的对象：解析失败时必须能看到模型
到底输出了什么。"
```

---

## Task 4: Agent 状态类型

**Files:**
- Create: `src/syndica/kernel/agent_types.py`
- Test: `tests/kernel/test_agent_types.py`

**Interfaces:**
- Consumes: 无
- Produces: `AgentStatus`（六态）、`ExitMode`（三档）、`AgentInfo` dataclass（含 `owner` / `heartbeat_at` 租约字段）

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_agent_types.py
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode


def test_six_states_exist():
    assert {s.value for s in AgentStatus} == {
        'idle',
        'running',
        'paused',
        'waiting',
        'aborting',
        'zombie',
    }


def test_three_exit_modes_exist():
    assert {m.value for m in ExitMode} == {'idle', 'zombie', 'exit'}


def test_info_defaults_to_idle_with_no_children():
    info = AgentInfo(agent_id='a1', name='Worker', agent_type='declarative')
    assert info.status is AgentStatus.IDLE
    assert info.children == []
    assert info.parent_id is None


def test_info_carries_lease_fields():
    """租约用于回收进程崩溃后留下的孤儿状态（spec §4.3）。"""
    info = AgentInfo(
        agent_id='a1',
        name='Worker',
        agent_type='declarative',
        owner='proc-1',
        heartbeat_at=123.0,
    )
    assert info.owner == 'proc-1'
    assert info.heartbeat_at == 123.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_agent_types.py -v`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/agent_types.py
"""Agent 的可序列化状态。

状态放 Registry 而不是 Agent 自身：Agent 阻塞或崩溃时无法自报状态
（spec §4.3），同操作系统把进程状态放在内核而非进程内。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ['AgentInfo', 'AgentStatus', 'ExitMode']


class AgentStatus(Enum):
    IDLE = 'idle'
    RUNNING = 'running'
    PAUSED = 'paused'
    WAITING = 'waiting'
    ABORTING = 'aborting'
    ZOMBIE = 'zombie'


class ExitMode(Enum):
    """一轮工作结束后的归宿。"""

    IDLE = 'idle'
    ZOMBIE = 'zombie'
    EXIT = 'exit'


@dataclass
class AgentInfo:
    agent_id: str
    name: str
    agent_type: str
    status: AgentStatus = AgentStatus.IDLE
    parent_id: str | None = None
    children: list[str] = field(default_factory=list[str])
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    owner: str | None = None
    heartbeat_at: float | None = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_agent_types.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/agent_types.py tests/kernel/test_agent_types.py
git commit -m "feat(kernel): Agent 状态类型，六态含 ZOMBIE

ZOMBIE 是刻意保留的能力而非未清理干净：停了事件循环、移了队列，但保留在
注册表因此仍可寻址，一条消息就能让它继续干。长跑与常驻场景依赖它。

租约字段（owner / heartbeat_at）现在就加：多进程下持有实例的进程崩溃会留下
状态为 RUNNING 却无人在跑的条目，而它「看起来正在运行」会阻止重新激活。"
```

---

## Task 5: AgentRegistry Protocol、内存实现与契约测试

**Files:**
- Create: `src/syndica/kernel/protocols/__init__.py`
- Create: `src/syndica/kernel/protocols/registry.py`
- Create: `src/syndica/kernel/stores/__init__.py`
- Create: `src/syndica/kernel/stores/memory_registry.py`
- Create: `src/syndica/kernel/contracts/__init__.py`
- Create: `src/syndica/kernel/contracts/registry.py`
- Test: `tests/contracts/test_memory_registry.py`

**Interfaces:**
- Consumes: `AgentInfo` / `AgentStatus`（Task 4）
- Produces:
  - `AgentRegistry` Protocol：`register` / `get` / `compare_and_set_status` / `update` / `children_of` / `list` / `unregister`
  - `InMemoryAgentRegistry()`
  - `AgentRegistryContract`——第三方实现继承它并提供 `registry` fixture 即可验证合规

- [ ] **Step 1: 写失败测试**

```python
# tests/contracts/test_memory_registry.py
import pytest

from syndica.kernel.contracts.registry import AgentRegistryContract
from syndica.kernel.stores.memory_registry import InMemoryAgentRegistry


class TestInMemoryAgentRegistry(AgentRegistryContract):
    @pytest.fixture
    def registry(self) -> InMemoryAgentRegistry:
        return InMemoryAgentRegistry()
```

- [ ] **Step 2: 写契约测试套件**

```python
# src/syndica/kernel/contracts/registry.py
"""AgentRegistry 契约。任何实现都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方
写 backend 时继承它并提供 registry fixture 即可验证合规。
"""

from __future__ import annotations

from syndica.kernel.agent_types import AgentInfo, AgentStatus
from syndica.kernel.protocols.registry import AgentRegistry

__all__ = ['AgentRegistryContract']


def _info(agent_id: str = 'a1', **kw: object) -> AgentInfo:
    return AgentInfo(agent_id=agent_id, name='Worker', agent_type='declarative', **kw)  # type: ignore[arg-type]


class AgentRegistryContract:
    """子类必须提供一个 `registry` fixture。"""

    async def test_register_then_get_roundtrips(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        got = await registry.get('a1')
        assert got is not None
        assert got.agent_id == 'a1'
        assert got.status is AgentStatus.IDLE

    async def test_get_unknown_returns_none(self, registry: AgentRegistry) -> None:
        assert await registry.get('missing') is None

    async def test_cas_succeeds_when_expectation_matches(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        ok = await registry.compare_and_set_status(
            'a1', expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        )
        assert ok is True
        got = await registry.get('a1')
        assert got is not None and got.status is AgentStatus.RUNNING

    async def test_cas_fails_when_expectation_stale(self, registry: AgentRegistry) -> None:
        """状态转换必须是 CAS：多进程下并发转换不得静默丢失。"""
        await registry.register(_info())
        await registry.compare_and_set_status(
            'a1', expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        )
        ok = await registry.compare_and_set_status(
            'a1', expected=AgentStatus.IDLE, new=AgentStatus.PAUSED
        )
        assert ok is False
        got = await registry.get('a1')
        assert got is not None and got.status is AgentStatus.RUNNING

    async def test_cas_on_unknown_agent_returns_false(self, registry: AgentRegistry) -> None:
        assert (
            await registry.compare_and_set_status(
                'missing', expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
            )
            is False
        )

    async def test_update_writes_single_writer_fields(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        await registry.update('a1', session_id='s1')
        got = await registry.get('a1')
        assert got is not None and got.session_id == 's1'

    async def test_children_of_reflects_parent_links(self, registry: AgentRegistry) -> None:
        await registry.register(_info('root'))
        await registry.register(_info('child', parent_id='root'))
        assert await registry.children_of('root') == ['child']

    async def test_list_filters_by_status(self, registry: AgentRegistry) -> None:
        await registry.register(_info('a1'))
        await registry.register(_info('a2', status=AgentStatus.ZOMBIE))
        zombies = await registry.list(status=AgentStatus.ZOMBIE)
        assert [i.agent_id for i in zombies] == ['a2']

    async def test_unregister_removes_entry(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        await registry.unregister('a1')
        assert await registry.get('a1') is None

    async def test_get_returns_snapshot_not_live_reference(
        self, registry: AgentRegistry
    ) -> None:
        """调用方改动返回值不得影响存储——否则远端实现与内存实现行为不一致。"""
        await registry.register(_info())
        got = await registry.get('a1')
        assert got is not None
        got.status = AgentStatus.ABORTING
        again = await registry.get('a1')
        assert again is not None and again.status is AgentStatus.IDLE
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/contracts/test_memory_registry.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.protocols'`

- [ ] **Step 4: 写 Protocol**

```python
# src/syndica/kernel/protocols/registry.py
"""AgentRegistry：Agent 可序列化状态的存储契约。

它只回答「这个 Agent 处于什么状态」；「这个 Agent 的对象在哪」由进程内的
LocalAgentTable 回答。两者分离是存储层可替换的地基（spec §4.3）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from syndica.kernel.agent_types import AgentInfo, AgentStatus

__all__ = ['AgentRegistry']


@runtime_checkable
class AgentRegistry(Protocol):
    async def register(self, info: AgentInfo) -> None: ...

    async def get(self, agent_id: str) -> AgentInfo | None:
        """返回快照。调用方修改返回值不得影响存储。"""
        ...

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        """状态转换。expected 不匹配或 agent 不存在时返回 False，不抛异常。"""
        ...

    async def update(self, agent_id: str, **fields: Any) -> None:
        """无条件写。仅限单一写者拥有的字段；多方可写的集合须用专用原子操作。"""
        ...

    async def children_of(self, agent_id: str) -> list[str]: ...

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]: ...

    async def unregister(self, agent_id: str) -> None: ...
```

```python
# src/syndica/kernel/protocols/__init__.py
from syndica.kernel.protocols.registry import AgentRegistry

__all__ = ['AgentRegistry']
```

- [ ] **Step 5: 写内存实现**

```python
# src/syndica/kernel/stores/memory_registry.py
"""AgentRegistry 的内存实现：单机默认，零依赖。"""

from __future__ import annotations

import copy

from typing import Any

from syndica.kernel.agent_types import AgentInfo, AgentStatus
from syndica.kernel.errors import ConfigError

__all__ = ['InMemoryAgentRegistry']


class InMemoryAgentRegistry:
    def __init__(self) -> None:
        self._infos: dict[str, AgentInfo] = {}

    async def register(self, info: AgentInfo) -> None:
        if info.agent_id in self._infos:
            raise ConfigError(
                expected=f'agent_id {info.agent_id!r} 尚未注册',
                received='该 agent_id 已存在于注册表',
                next_action='换一个 agent_id，或先 unregister 旧条目',
            )
        self._infos[info.agent_id] = copy.deepcopy(info)
        if info.parent_id and info.parent_id in self._infos:
            parent = self._infos[info.parent_id]
            if info.agent_id not in parent.children:
                parent.children.append(info.agent_id)

    async def get(self, agent_id: str) -> AgentInfo | None:
        info = self._infos.get(agent_id)
        return copy.deepcopy(info) if info is not None else None

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        info = self._infos.get(agent_id)
        if info is None or info.status is not expected:
            return False
        info.status = new
        return True

    async def update(self, agent_id: str, **fields: Any) -> None:
        info = self._infos.get(agent_id)
        if info is None:
            raise ConfigError(
                expected=f'agent_id {agent_id!r} 已注册',
                received='注册表中不存在该条目',
                next_action='先 register，或检查 agent_id 是否拼错',
            )
        for key, value in fields.items():
            if not hasattr(info, key):
                raise ConfigError(
                    expected=f'AgentInfo 拥有字段 {key!r}',
                    received=f'AgentInfo 无此字段；可用字段: {sorted(vars(info))}',
                    next_action='改用已存在的字段名，或把新字段加进 AgentInfo',
                )
            setattr(info, key, value)

    async def children_of(self, agent_id: str) -> list[str]:
        info = self._infos.get(agent_id)
        return list(info.children) if info is not None else []

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]:
        result = [
            copy.deepcopy(info)
            for info in self._infos.values()
            if (status is None or info.status is status)
            and (parent_id is None or info.parent_id == parent_id)
        ]
        return result

    async def unregister(self, agent_id: str) -> None:
        info = self._infos.pop(agent_id, None)
        if info is not None and info.parent_id in self._infos:
            parent = self._infos[info.parent_id]
            if agent_id in parent.children:
                parent.children.remove(agent_id)
```

```python
# src/syndica/kernel/stores/__init__.py
from syndica.kernel.stores.memory_registry import InMemoryAgentRegistry

__all__ = ['InMemoryAgentRegistry']
```

```python
# src/syndica/kernel/contracts/__init__.py
from syndica.kernel.contracts.registry import AgentRegistryContract

__all__ = ['AgentRegistryContract']
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/contracts/test_memory_registry.py -v`
Expected: 10 passed

- [ ] **Step 7: 提交**

```bash
git add src/syndica/kernel/protocols src/syndica/kernel/stores src/syndica/kernel/contracts tests/contracts/test_memory_registry.py
git commit -m "feat(kernel): AgentRegistry Protocol、内存实现与契约测试套件

契约测试随发行版发布，第三方写 backend 继承它即可验证合规——这是「四面
可替换」唯一的证明方式，也是比散文更准确的契约文档。

两条契约值得单独说：
- compare_and_set_status 是 CAS 而非直接赋值。单进程下赋值可行，多进程下
  两个执行体同时改同一 Agent 的状态会丢转换（挂起与中止相撞）
- get 必须返回快照。若返回活引用，内存实现能改到存储、远端实现不能，
  两者行为分叉且只在换后端时暴露"
```

---

## Task 6: MessageBroker Protocol、内存实现与契约测试

**Files:**
- Create: `src/syndica/kernel/message.py`
- Create: `src/syndica/kernel/protocols/broker.py`
- Create: `src/syndica/kernel/stores/memory_broker.py`
- Create: `src/syndica/kernel/contracts/broker.py`
- Modify: `src/syndica/kernel/protocols/__init__.py`、`stores/__init__.py`、`contracts/__init__.py`
- Test: `tests/contracts/test_memory_broker.py`

**Interfaces:**
- Consumes: `TrustLevel`（Task 3）、`ConfigError`（Task 2）
- Produces:
  - `Trigger`（`PENDING` / `DEFERRED`）、`Message(id, sender, receiver, content, trigger, correlation_id, trust)`、`DeliveryResult(ok, message_id, reason)`
  - `MessageBroker` Protocol：`ensure_queue` / `publish` / `consume` / `wait_for_messages` / `take_by_correlation` / `remove_queue` / `on_missing_queue`
  - `InMemoryMessageBroker()`、`MessageBrokerContract`

- [ ] **Step 1: 写失败测试**

```python
# tests/contracts/test_memory_broker.py
import pytest

from syndica.kernel.contracts.broker import MessageBrokerContract
from syndica.kernel.stores.memory_broker import InMemoryMessageBroker


class TestInMemoryMessageBroker(MessageBrokerContract):
    @pytest.fixture
    def broker(self) -> InMemoryMessageBroker:
        return InMemoryMessageBroker()
```

- [ ] **Step 2: 写消息类型**

```python
# src/syndica/kernel/message.py
"""消息类型。

两类消息统一经 Runtime.send 发出，由 trigger 区分消费时机（spec §4.5）：
PENDING 在下一轮 turn 开始前消费；DEFERRED 等整个 Agent 结束后依次消费。
"""

from __future__ import annotations

import uuid

from dataclasses import dataclass, field
from enum import Enum

from syndica.kernel.context import TrustLevel

__all__ = ['DeliveryResult', 'Message', 'Trigger']


class Trigger(Enum):
    PENDING = 'pending'
    DEFERRED = 'deferred'


@dataclass
class Message:
    sender: str
    receiver: str
    content: str
    trigger: Trigger = Trigger.PENDING
    correlation_id: str | None = None
    trust: TrustLevel = TrustLevel.TRUSTED
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class DeliveryResult:
    """投递结果。失败返回结构化结果而不抛异常，便于识别可重试的瞬时错误。"""

    ok: bool
    message_id: str | None = None
    reason: str | None = None
```

- [ ] **Step 3: 写契约测试套件**

```python
# src/syndica/kernel/contracts/broker.py
"""MessageBroker 契约。任何实现都必须通过。"""

from __future__ import annotations

import asyncio

from syndica.kernel.message import Message
from syndica.kernel.protocols.broker import MessageBroker

__all__ = ['MessageBrokerContract']


def _msg(content: str = 'hi', **kw: object) -> Message:
    return Message(sender='s', receiver='a1', content=content, **kw)  # type: ignore[arg-type]


class MessageBrokerContract:
    """子类必须提供一个 `broker` fixture。"""

    async def test_publish_then_consume_preserves_order(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('one'))
        await broker.publish('a1', _msg('two'))
        got = await broker.consume('a1')
        assert [m.content for m in got] == ['one', 'two']

    async def test_consume_is_exactly_once(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        assert len(await broker.consume('a1')) == 1
        assert await broker.consume('a1') == []

    async def test_consume_respects_max_n(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        for i in range(3):
            await broker.publish('a1', _msg(str(i)))
        first = await broker.consume('a1', max_n=2)
        assert [m.content for m in first] == ['0', '1']
        assert [m.content for m in await broker.consume('a1')] == ['2']

    async def test_wait_for_messages_returns_true_when_already_queued(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        assert await broker.wait_for_messages('a1', timeout=0.1) is True

    async def test_wait_for_messages_wakes_on_publish(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')

        async def publish_soon() -> None:
            await asyncio.sleep(0.01)
            await broker.publish('a1', _msg())

        task = asyncio.create_task(publish_soon())
        assert await broker.wait_for_messages('a1', timeout=1.0) is True
        await task

    async def test_wait_for_messages_returns_false_on_timeout(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        assert await broker.wait_for_messages('a1', timeout=0.01) is False

    async def test_take_by_correlation_leaves_others_queued(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('other'))
        await broker.publish('a1', _msg('answer', correlation_id='c1'))
        got = await broker.take_by_correlation('a1', 'c1', timeout=0.1)
        assert got is not None and got.content == 'answer'
        assert [m.content for m in await broker.consume('a1')] == ['other']

    async def test_take_by_correlation_times_out_without_match(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('other'))
        assert await broker.take_by_correlation('a1', 'nope', timeout=0.01) is None

    async def test_missing_queue_triggers_callback(self, broker: MessageBroker) -> None:
        activated: list[str] = []

        async def activate(agent_id: str) -> bool:
            activated.append(agent_id)
            await broker.ensure_queue(agent_id)
            return True

        broker.on_missing_queue(activate)
        result = await broker.publish('lazy', _msg())
        assert result.ok is True
        assert activated == ['lazy']

    async def test_publish_to_missing_queue_fails_when_no_callback(
        self, broker: MessageBroker
    ) -> None:
        """失败返回结构化结果，不抛异常。"""
        result = await broker.publish('nobody', _msg())
        assert result.ok is False
        assert result.reason is not None

    async def test_remove_queue_discards_pending(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        await broker.remove_queue('a1')
        result = await broker.publish('a1', _msg())
        assert result.ok is False
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/contracts/test_memory_broker.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.protocols.broker'`

- [ ] **Step 5: 写 Protocol**

```python
# src/syndica/kernel/protocols/broker.py
"""MessageBroker：Agent 邮箱的存储契约。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from syndica.kernel.message import DeliveryResult, Message

__all__ = ['MessageBroker', 'MissingQueueCallback']

MissingQueueCallback = Callable[[str], Awaitable[bool]]


@runtime_checkable
class MessageBroker(Protocol):
    async def ensure_queue(self, agent_id: str) -> None: ...

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        """投递。队列不存在时先走 on_missing_queue 回调；仍失败则返回 ok=False。"""
        ...

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        """批量取出。max_n=0 表示全取。"""
        ...

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool: ...

    async def take_by_correlation(
        self, agent_id: str, correlation_id: str, timeout: float | None
    ) -> Message | None:
        """按关联 ID 取单条，其余留在队列。

        刻意不接受任意谓词：谓词无法下推到远端队列，分布式实现只能全量取回
        本地过滤再放回，既慢又有竞态。
        """
        ...

    async def remove_queue(self, agent_id: str) -> None: ...

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        """注册按需激活回调。「队列不存在」是懒启动与 ZOMBIE 复活的触发点。"""
        ...
```

- [ ] **Step 6: 写内存实现**

```python
# src/syndica/kernel/stores/memory_broker.py
"""MessageBroker 的内存实现：asyncio 队列，单机默认。"""

from __future__ import annotations

import asyncio

from syndica.kernel.message import DeliveryResult, Message
from syndica.kernel.protocols.broker import MissingQueueCallback

__all__ = ['InMemoryMessageBroker']


class InMemoryMessageBroker:
    def __init__(self) -> None:
        self._queues: dict[str, list[Message]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._callbacks: list[MissingQueueCallback] = []

    async def ensure_queue(self, agent_id: str) -> None:
        self._queues.setdefault(agent_id, [])
        self._events.setdefault(agent_id, asyncio.Event())

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        if agent_id not in self._queues:
            for cb in self._callbacks:
                if await cb(agent_id):
                    break
        if agent_id not in self._queues:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 有可投递的队列。'
                    f' 实际: 队列不存在，且没有 on_missing_queue 回调认领它。'
                    f' 下一步: 先 ensure_queue，或注册一个能激活该 agent 的回调。'
                ),
            )
        self._queues[agent_id].append(message)
        self._events[agent_id].set()
        return DeliveryResult(ok=True, message_id=message.id)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        queue = self._queues.get(agent_id)
        if not queue:
            return []
        taken = queue[:] if max_n <= 0 else queue[:max_n]
        del queue[: len(taken)]
        if not queue:
            self._events[agent_id].clear()
        return taken

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool:
        if self._queues.get(agent_id):
            return True
        event = self._events.get(agent_id)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return bool(self._queues.get(agent_id))

    async def take_by_correlation(
        self, agent_id: str, correlation_id: str, timeout: float | None
    ) -> Message | None:
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while True:
            queue = self._queues.get(agent_id, [])
            for index, message in enumerate(queue):
                if message.correlation_id == correlation_id:
                    return queue.pop(index)
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining is not None and remaining <= 0:
                return None
            event = self._events.get(agent_id)
            if event is None:
                return None
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return None

    async def remove_queue(self, agent_id: str) -> None:
        self._queues.pop(agent_id, None)
        self._events.pop(agent_id, None)

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._callbacks.append(cb)
```

更新三个 `__init__.py` 的导出：

```python
# src/syndica/kernel/protocols/__init__.py
from syndica.kernel.protocols.broker import MessageBroker, MissingQueueCallback
from syndica.kernel.protocols.registry import AgentRegistry

__all__ = ['AgentRegistry', 'MessageBroker', 'MissingQueueCallback']
```

```python
# src/syndica/kernel/stores/__init__.py
from syndica.kernel.stores.memory_broker import InMemoryMessageBroker
from syndica.kernel.stores.memory_registry import InMemoryAgentRegistry

__all__ = ['InMemoryAgentRegistry', 'InMemoryMessageBroker']
```

```python
# src/syndica/kernel/contracts/__init__.py
from syndica.kernel.contracts.broker import MessageBrokerContract
from syndica.kernel.contracts.registry import AgentRegistryContract

__all__ = ['AgentRegistryContract', 'MessageBrokerContract']
```

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run pytest tests/contracts/ -v`
Expected: 21 passed（registry 10 + broker 11）

- [ ] **Step 8: 提交**

```bash
git add src/syndica/kernel/message.py src/syndica/kernel/protocols src/syndica/kernel/stores src/syndica/kernel/contracts tests/contracts/test_memory_broker.py
git commit -m "feat(kernel): MessageBroker Protocol、内存实现与契约测试

take_by_correlation 按关联 ID 而非任意谓词匹配：谓词无法下推到远端队列，
分布式实现只能全量取回本地过滤再放回，既慢又有竞态。代价是表达力变窄
（只能等「对某条特定请求的回复」），收益是分布式实现从不可行变为自然。

publish 失败返回结构化 DeliveryResult 而不抛异常，调用方才能区分可重试的
瞬时错误。on_missing_queue 是懒启动与 ZOMBIE 复活的统一触发点。"
```

---

## Task 7: LocalAgentTable

**Files:**
- Create: `src/syndica/kernel/local_table.py`
- Test: `tests/kernel/test_local_table.py`

**Interfaces:**
- Consumes: 无（刻意不依赖 `BaseAgent`，避免循环导入；用泛型持有）
- Produces: `LocalAgentTable`：`put(agent_id, agent, task=None)` / `get(agent_id)` / `get_task(agent_id)` / `set_task` / `pop(agent_id)` / `agent_ids()`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_local_table.py
import asyncio

from syndica.kernel.local_table import LocalAgentTable


class _FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name


def test_put_then_get_returns_same_object():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    agent = _FakeAgent('w1')
    table.put('a1', agent)
    assert table.get('a1') is agent


def test_get_unknown_returns_none():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    assert table.get('missing') is None


async def test_task_is_stored_alongside_agent():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))

    async def noop() -> None:
        return None

    task = asyncio.create_task(noop())
    table.set_task('a1', task)
    assert table.get_task('a1') is task
    await task


def test_pop_removes_entry():
    table: LocalAgentTable[_FakeAgent] = LocalAgentTable()
    table.put('a1', _FakeAgent('w1'))
    table.pop('a1')
    assert table.get('a1') is None
    assert table.agent_ids() == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_local_table.py -v`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/local_table.py
"""进程内的 Agent 实例表。

与 AgentRegistry 的分工：Registry 存可序列化状态、可远程共享；本表存运行时
对象（Agent 实例、asyncio 任务），只在持有它们的进程内有意义，因此不进
Protocol（spec §4.3）。多进程部署时前者共享，后者各进程只有自己那部分。
"""

from __future__ import annotations

import asyncio

from typing import Generic, TypeVar

__all__ = ['LocalAgentTable']

AgentT = TypeVar('AgentT')


class LocalAgentTable(Generic[AgentT]):
    def __init__(self) -> None:
        self._agents: dict[str, AgentT] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def put(self, agent_id: str, agent: AgentT, task: asyncio.Task[None] | None = None) -> None:
        self._agents[agent_id] = agent
        if task is not None:
            self._tasks[agent_id] = task

    def get(self, agent_id: str) -> AgentT | None:
        return self._agents.get(agent_id)

    def set_task(self, agent_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[agent_id] = task

    def get_task(self, agent_id: str) -> asyncio.Task[None] | None:
        return self._tasks.get(agent_id)

    def pop(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)
        self._tasks.pop(agent_id, None)

    def agent_ids(self) -> list[str]:
        return list(self._agents)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_local_table.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/local_table.py tests/kernel/test_local_table.py
git commit -m "feat(kernel): 进程内 Agent 实例表

Registry 与本表的分离是存储层可替换的地基：Agent 实例与 asyncio 任务不可
序列化，把它们和状态混在一个 registry 里，Protocol 化的第一步就走不通。

用泛型持有 Agent 而不 import BaseAgent，避免循环导入。"
```

---

## Task 8: 进程内事件流

**Files:**
- Create: `src/syndica/kernel/events.py`
- Test: `tests/kernel/test_events.py`

**Interfaces:**
- Consumes: 无
- Produces: `Event(kind, agent_id, payload)`、`EventBus`：`subscribe() -> AsyncIterator[Event]` / `emit(event)` / `close()`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_events.py
import asyncio

from syndica.kernel.events import Event, EventBus


async def test_subscriber_receives_emitted_event():
    bus = EventBus()
    received: list[Event] = []

    async def consume() -> None:
        async for event in bus.subscribe():
            received.append(event)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await bus.emit(Event(kind='turn_start', agent_id='a1', payload={'turn': 1}))
    await task
    assert received[0].kind == 'turn_start'


async def test_two_subscribers_both_receive():
    """CLI 与 server 会同时订阅同一条流，不能互相抢消息。"""
    bus = EventBus()
    got: list[str] = []

    async def consume(tag: str) -> None:
        async for _ in bus.subscribe():
            got.append(tag)
            break

    tasks = [asyncio.create_task(consume('a')), asyncio.create_task(consume('b'))]
    await asyncio.sleep(0)
    await bus.emit(Event(kind='turn_end', agent_id='a1', payload={}))
    await asyncio.gather(*tasks)
    assert sorted(got) == ['a', 'b']


async def test_emit_without_subscribers_does_not_block():
    bus = EventBus()
    await asyncio.wait_for(
        bus.emit(Event(kind='turn_start', agent_id='a1', payload={})), timeout=0.1
    )


async def test_close_ends_subscription():
    bus = EventBus()
    seen = 0

    async def consume() -> None:
        nonlocal seen
        async for _ in bus.subscribe():
            seen += 1

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await bus.close()
    await asyncio.wait_for(task, timeout=1.0)
    assert seen == 0


async def test_emit_after_close_is_a_noop():
    bus = EventBus()
    await bus.close()
    await bus.emit(Event(kind='turn_start', agent_id='a1', payload={}))


async def test_subscribe_after_close_ends_immediately():
    """否则迟到的订阅者会永久挂在空队列上等一个永不到来的事件。"""
    bus = EventBus()
    await bus.close()
    seen = [event async for event in bus.subscribe()]
    assert seen == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_events.py -v`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/events.py
"""进程内事件流。

事件流首先是进程内可订阅的，HTTP SSE 只是它的一个出口（spec §9.1 末条）。
若只在 HTTP 层存在，CLI 的交互模式就只能自己再实现一套，那正是「chat 必须
是 run --session 的外壳」所要禁止的分叉。

多订阅者各自持有独立队列：CLI 与 server 会同时订阅，不能互相抢消息。
"""

from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

__all__ = ['Event', 'EventBus']


@dataclass(frozen=True)
class Event:
    kind: str
    agent_id: str
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


class EventBus:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[Event | None]] = []
        self._closed = False

    async def emit(self, event: Event) -> None:
        """向所有订阅者投递。

        关闭后与无订阅者时都直接丢弃：可观测性不得拖垮主流程，所以这里既不
        抛异常也不阻塞。
        """
        if self._closed:
            return
        for queue in list(self._queues):
            queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        """订阅事件。已关闭时立即结束，不会挂住调用方。"""
        if self._closed:
            return
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._queues.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            if queue in self._queues:
                self._queues.remove(queue)

    async def close(self) -> None:
        self._closed = True
        for queue in list(self._queues):
            queue.put_nowait(None)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_events.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/events.py tests/kernel/test_events.py
git commit -m "feat(kernel): 进程内事件流

事件流必须首先是进程内可订阅的，HTTP SSE 只是它的一个出口。若只在 HTTP
层存在，CLI 交互模式只能自己再实现一套消费逻辑，就出现了「交互里能用、
脚本里不行」这种极难排查的分叉。

多订阅者各持独立队列而非共享一个：CLI 与 server 会同时订阅同一条流。
emit 在无订阅者时直接丢弃——可观测性不得拖垮主流程。"
```

---

## Task 9: Hook 骨架

**Files:**
- Create: `src/syndica/kernel/hooks/__init__.py`
- Create: `src/syndica/kernel/hooks/junctions.py`
- Create: `src/syndica/kernel/hooks/result.py`
- Create: `src/syndica/kernel/hooks/center.py`
- Test: `tests/kernel/test_hooks.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `Junction` 枚举（本计划需要的切点）、`INTERCEPTABLE`、`OBSERVABLE`
  - `HookResult(skip, modified, result)`，`.get(key, default)`
  - `HooksCenter`：`register(junction, cb)` / `trigger(junction, **kwargs) -> HookResult`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_hooks.py
import pytest

from syndica.kernel.hooks import HooksCenter, HookResult, Junction


async def test_observable_hooks_all_run():
    center = HooksCenter()
    calls: list[str] = []

    async def first(**_: object) -> None:
        calls.append('first')

    async def second(**_: object) -> None:
        calls.append('second')

    center.register(Junction.TURN_START, first)
    center.register(Junction.TURN_START, second)
    await center.trigger(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['first', 'second']


async def test_interceptable_hook_can_modify_kwargs():
    center = HooksCenter()

    async def rewrite(**kwargs: object) -> HookResult:
        return HookResult(modified={'request': 'rewritten'})

    center.register(Junction.PRE_LLM_REQUEST, rewrite)
    result = await center.trigger(Junction.PRE_LLM_REQUEST, request='original')
    assert result.get('request', 'original') == 'rewritten'


async def test_modified_kwargs_reach_the_next_hook():
    center = HooksCenter()
    seen: list[object] = []

    async def first(**kwargs: object) -> HookResult:
        return HookResult(modified={'request': 'from-first'})

    async def second(**kwargs: object) -> HookResult:
        seen.append(kwargs['request'])
        return HookResult()

    center.register(Junction.PRE_LLM_REQUEST, first)
    center.register(Junction.PRE_LLM_REQUEST, second)
    await center.trigger(Junction.PRE_LLM_REQUEST, request='original')
    assert seen == ['from-first']


async def test_skip_stops_subsequent_interceptable_hooks():
    center = HooksCenter()
    calls: list[str] = []

    async def blocker(**_: object) -> HookResult:
        calls.append('blocker')
        return HookResult(skip=True, result='substitute')

    async def never(**_: object) -> HookResult:
        calls.append('never')
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, blocker)
    center.register(Junction.PRE_TOOL_USE, never)
    result = await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
    assert calls == ['blocker']
    assert result.skip is True
    assert result.result == 'substitute'


async def test_trigger_with_no_hooks_returns_empty_result():
    center = HooksCenter()
    result = await center.trigger(Junction.TURN_END, turn=1)
    assert result.skip is False
    assert result.modified == {}


async def test_registering_unknown_junction_is_rejected():
    center = HooksCenter()

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(Exception, match='Junction'):
        center.register('turn_start', cb)  # type: ignore[arg-type]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_hooks.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.hooks'`

- [ ] **Step 3: 写切点与结果类型**

```python
# src/syndica/kernel/hooks/junctions.py
"""切点定义。

切点名计入公开 API，变更即破坏性变更。本计划只落地单 Agent 一轮所需的切点，
其余（agent_spawn / message_injected / 背景任务等）随后续计划补齐。

可拦截切点串行执行且可改 kwargs；可观察切点可并行。
"""

from __future__ import annotations

from enum import Enum

__all__ = ['INTERCEPTABLE', 'OBSERVABLE', 'Junction']


class Junction(Enum):
    AGENT_START = 'agent_start'
    AGENT_END = 'agent_end'
    AGENT_ERROR = 'agent_error'
    TURN_START = 'turn_start'
    TURN_END = 'turn_end'
    PRE_TOOL_USE = 'pre_tool_use'
    POST_TOOL_USE = 'post_tool_use'
    PRE_LLM_REQUEST = 'pre_llm_request'
    POST_LLM_RESPONSE = 'post_llm_response'
    CONTEXT_OVERFLOW = 'context_overflow'
    LLM_FAILURE = 'llm_failure'


INTERCEPTABLE = frozenset(
    {
        Junction.PRE_TOOL_USE,
        Junction.POST_TOOL_USE,
        Junction.PRE_LLM_REQUEST,
        Junction.POST_LLM_RESPONSE,
        Junction.CONTEXT_OVERFLOW,
        Junction.LLM_FAILURE,
    }
)

OBSERVABLE = frozenset(set(Junction) - INTERCEPTABLE)
```

```python
# src/syndica/kernel/hooks/result.py
"""中间件契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ['HookResult']


@dataclass
class HookResult:
    skip: bool = False
    modified: dict[str, Any] = field(default_factory=dict[str, Any])
    result: Any = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.modified.get(key, default)
```

- [ ] **Step 4: 写 HooksCenter**

```python
# src/syndica/kernel/hooks/center.py
"""切点分发。

可拦截切点串行执行，前一个 hook 的 modified 会合并进后续 hook 的 kwargs；
skip 终止后续可拦截 hook。可观察切点并行执行，异常只记录不影响主流程。
"""

from __future__ import annotations

import asyncio
import logging

from collections.abc import Awaitable, Callable
from typing import Any

from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks.junctions import INTERCEPTABLE, Junction
from syndica.kernel.hooks.result import HookResult

__all__ = ['HookCallback', 'HooksCenter']

logger = logging.getLogger(__name__)

HookCallback = Callable[..., Awaitable[HookResult | None]]


class HooksCenter:
    def __init__(self) -> None:
        self._hooks: dict[Junction, list[HookCallback]] = {j: [] for j in Junction}

    def register(self, junction: Junction, cb: HookCallback) -> None:
        if not isinstance(junction, Junction):
            raise ConfigError(
                expected='junction 是 Junction 枚举成员',
                received=f'收到 {type(junction).__name__} 类型的 {junction!r}',
                next_action='改用 Junction.<NAME>；切点名计入公开 API，不接受裸字符串',
            )
        self._hooks[junction].append(cb)

    async def trigger(self, junction: Junction, **kwargs: Any) -> HookResult:
        hooks = self._hooks[junction]
        if not hooks:
            return HookResult()
        if junction in INTERCEPTABLE:
            return await self._trigger_serial(hooks, **kwargs)
        await self._trigger_parallel(junction, hooks, **kwargs)
        return HookResult()

    async def _trigger_serial(self, hooks: list[HookCallback], **kwargs: Any) -> HookResult:
        merged: dict[str, Any] = {}
        current = dict(kwargs)
        for cb in hooks:
            outcome = await cb(**current)
            if outcome is None:
                continue
            merged.update(outcome.modified)
            current.update(outcome.modified)
            if outcome.skip:
                return HookResult(skip=True, modified=merged, result=outcome.result)
        return HookResult(modified=merged)

    async def _trigger_parallel(
        self, junction: Junction, hooks: list[HookCallback], **kwargs: Any
    ) -> None:
        results = await asyncio.gather(
            *(cb(**kwargs) for cb in hooks), return_exceptions=True
        )
        for outcome in results:
            if isinstance(outcome, BaseException):
                logger.exception('observable hook failed at %s', junction.value, exc_info=outcome)
```

```python
# src/syndica/kernel/hooks/__init__.py
from syndica.kernel.hooks.center import HookCallback, HooksCenter
from syndica.kernel.hooks.junctions import INTERCEPTABLE, OBSERVABLE, Junction
from syndica.kernel.hooks.result import HookResult

__all__ = [
    'INTERCEPTABLE',
    'OBSERVABLE',
    'HookCallback',
    'HookResult',
    'HooksCenter',
    'Junction',
]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_hooks.py -v`
Expected: 6 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/hooks tests/kernel/test_hooks.py
git commit -m "feat(kernel): Hook 骨架，六个可拦截切点

Hook 机制必须在 turn 循环之前就位。turn 循环有多处要触发切点，若机制后补
就得改主循环——而主循环是全仓最不该反复动的一段。

可拦截切点串行且 modified 会合并进后续 hook 的 kwargs（这是中间件语义的
关键，只让第一个 hook 生效就退化成了普通回调）；可观察切点并行，异常只
记录不影响主流程。"
```

---

## Task 10: LLM 抽象与脚本化假 LLM

**Files:**
- Create: `src/syndica/kernel/llm/__init__.py`
- Create: `src/syndica/kernel/llm/base.py`
- Create: `src/syndica/kernel/llm/scripted.py`
- Test: `tests/kernel/test_llm.py`

**Interfaces:**
- Consumes: `ContextBlock` / `ToolCall`（Task 3）、`LLMResponseError`（Task 2）
- Produces:
  - `LLMConfig(provider, model, client_args, request_args, timeout, context_window)` frozen，`.derive(**changes)`
  - `LLM` Protocol：`create(messages, **kwargs) -> ContextBlock`
  - `ContextOverflowError`
  - `ScriptedLLM(script)`：按序返回预设响应；`ScriptedTurn(content=None, tool_calls=None, raises=None)`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_llm.py
import pytest

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.llm import ContextOverflowError, LLMConfig, ScriptedLLM, ScriptedTurn


def test_config_is_frozen():
    cfg = LLMConfig(provider='scripted', model='m1')
    with pytest.raises(Exception):
        cfg.model = 'm2'  # type: ignore[misc]


def test_derive_returns_new_config():
    cfg = LLMConfig(provider='scripted', model='m1', request_args={'temperature': 0})
    derived = cfg.derive(model='m2')
    assert derived.model == 'm2'
    assert derived.request_args == {'temperature': 0}
    assert cfg.model == 'm1'


async def test_scripted_llm_returns_turns_in_order():
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]),
            ScriptedTurn(content='done'),
        ]
    )
    first = await llm.create([ContextBlock(role='user', content='go')])
    assert first.tool_calls[0].name == 'Echo'
    second = await llm.create([ContextBlock(role='user', content='go')])
    assert second.content == 'done'


async def test_scripted_llm_can_raise_to_test_failure_paths():
    """失败路径无法用真模型按需触发，这是脚本化假 LLM 存在的核心理由。"""
    llm = ScriptedLLM([ScriptedTurn(raises=ContextOverflowError(
        expected='请求在上下文窗口内',
        received='超出窗口',
        next_action='压缩上下文后重试',
    ))])
    with pytest.raises(ContextOverflowError):
        await llm.create([ContextBlock(role='user', content='go')])


async def test_scripted_llm_exhaustion_is_a_clear_error():
    llm = ScriptedLLM([ScriptedTurn(content='only one')])
    await llm.create([])
    with pytest.raises(Exception, match='脚本已用尽'):
        await llm.create([])


async def test_scripted_llm_records_received_messages():
    llm = ScriptedLLM([ScriptedTurn(content='ok')])
    await llm.create([ContextBlock(role='system', content='SP'), ContextBlock(role='user', content='q')])
    assert [b.role for b in llm.calls[0]] == ['system', 'user']
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_llm.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.llm'`

- [ ] **Step 3: 写 LLM 抽象**

```python
# src/syndica/kernel/llm/base.py
"""LLM 抽象。

内核不认识具体 provider：provider 只是字符串标识，实现由 backend 注册。
请求参数归实例（config 提供默认，调用方只传单次覆盖），否则每个调用点都要
重复一堆参数。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import SyndicaError

__all__ = ['ContextOverflowError', 'LLM', 'LLMConfig']


class ContextOverflowError(SyndicaError):
    """请求超出上下文窗口。触发 CONTEXT_OVERFLOW 切点，由压缩能力处理。"""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    client_args: dict[str, Any] = field(default_factory=dict[str, Any])
    request_args: dict[str, Any] = field(default_factory=dict[str, Any])
    timeout: float = 600.0
    context_window: int | None = None

    def derive(self, **changes: Any) -> LLMConfig:
        """派生必须走此方法：影响身份的参数直接改会让缓存键与计费归属静默错位。"""
        return replace(self, **changes)


@runtime_checkable
class LLM(Protocol):
    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        """返回一个 assistant block。超窗抛 ContextOverflowError。"""
        ...
```

- [ ] **Step 4: 写脚本化假 LLM**

```python
# src/syndica/kernel/llm/scripted.py
"""脚本化假 LLM。

它是唯一能测试失败路径的手段：LLM 异常三级分类、空响应守卫、中断保留部分
回复这些分支，无法要求真实模型按需触发。同时它让编排逻辑的测试快速、确定、
零成本（spec §13.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.errors import ConfigError

__all__ = ['ScriptedLLM', 'ScriptedTurn']


@dataclass
class ScriptedTurn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list[ToolCall])
    raises: BaseException | None = None


class ScriptedLLM:
    def __init__(self, script: list[ScriptedTurn]) -> None:
        self._script = list(script)
        self._index = 0
        self.calls: list[list[ContextBlock]] = []

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.calls.append(list(messages))
        if self._index >= len(self._script):
            raise ConfigError(
                expected=f'脚本至少有 {self._index + 1} 轮',
                received=f'脚本已用尽，只提供了 {len(self._script)} 轮',
                next_action='给 ScriptedLLM 补一轮 ScriptedTurn，或检查被测循环是否多跑了一轮',
            )
        turn = self._script[self._index]
        self._index += 1
        if turn.raises is not None:
            raise turn.raises
        return ContextBlock(
            role='assistant', content=turn.content, tool_calls=list(turn.tool_calls)
        )
```

```python
# src/syndica/kernel/llm/__init__.py
from syndica.kernel.llm.base import LLM, ContextOverflowError, LLMConfig
from syndica.kernel.llm.scripted import ScriptedLLM, ScriptedTurn

__all__ = ['LLM', 'ContextOverflowError', 'LLMConfig', 'ScriptedLLM', 'ScriptedTurn']
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_llm.py -v`
Expected: 6 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/llm tests/kernel/test_llm.py
git commit -m "feat(kernel): LLM 抽象与脚本化假 LLM

ScriptedLLM 不是权宜之计而是必需品：LLM 异常三级分类、空响应守卫这些分支
无法要求真实模型按需触发，没有它这些最容易出 bug 的路径就是零覆盖。它也
让编排测试快速、确定、零成本——贡献者与 CI 都不需要 API 密钥。

LLMConfig 用 frozen + derive：影响身份的参数（provider / model）直接改会让
缓存键与计费归属静默错位，而这类错位不报错、只在账单和缓存命中率上体现。"
```

---

## Task 11: Tool 基类与 schema 生成

**Files:**
- Create: `src/syndica/kernel/tool.py`
- Test: `tests/kernel/test_tool.py`

**Interfaces:**
- Consumes: `ArtifactRef` / `TrustLevel`（Task 3）、`ConfigError`（Task 2）
- Produces:
  - `ToolResult(output, meta, artifacts, trust)`
  - `Tool` 抽象基类：`name` / `description` / `parameters` / `schema` / `execute(args) -> ToolResult` / `is_concurrency_safe(args) -> bool`（默认 `False`）
  - schema 三级回退：`__tool_schema__` → `__tool_model__` → 反射 `execute` 签名与 docstring

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_tool.py
import pytest

from pydantic import BaseModel

from syndica.kernel.context import TrustLevel
from syndica.kernel.tool import Tool, ToolResult


class _Reflected(Tool):
    async def execute(self, text: str, times: int = 1) -> ToolResult:
        """把 text 重复 times 次。

        text: 要重复的内容
        times: 重复次数
        """
        return ToolResult(output=text * times)


class _ModelArgs(BaseModel):
    path: str


class _WithModel(Tool):
    __tool_model__ = _ModelArgs

    async def execute(self, **kwargs: object) -> ToolResult:
        return ToolResult(output=str(kwargs['path']))


class _HandWritten(Tool):
    __tool_schema__ = {
        'name': 'Custom',
        'description': '手写描述',
        'parameters': {'type': 'object', 'properties': {}},
    }

    async def execute(self, **kwargs: object) -> ToolResult:
        return ToolResult(output='ok')


def test_name_defaults_to_class_name():
    assert _Reflected().name == 'Reflected'


def test_schema_from_reflection_includes_params_and_required():
    schema = _Reflected().schema
    props = schema['parameters']['properties']
    assert props['text']['type'] == 'string'
    assert props['times']['type'] == 'integer'
    assert schema['parameters']['required'] == ['text']


def test_schema_from_reflection_uses_docstring_summary():
    assert '重复' in _Reflected().schema['description']


def test_tool_model_takes_priority_over_reflection():
    schema = _WithModel().schema
    assert 'path' in schema['parameters']['properties']


def test_hand_written_schema_takes_highest_priority():
    assert _HandWritten().schema['description'] == '手写描述'


def test_result_separates_model_output_from_workflow_meta():
    result = ToolResult(output='for model', meta={'rows': 3})
    assert result.output == 'for model'
    assert result.meta['rows'] == 3


def test_result_defaults_trusted_and_no_artifacts():
    result = ToolResult(output='x')
    assert result.trust is TrustLevel.TRUSTED
    assert result.artifacts == []


def test_concurrency_safe_defaults_to_false():
    """安全判定从严：并发执行两个改同一文件的工具后果不可逆，串行只是慢。"""
    assert _Reflected().is_concurrency_safe({}) is False


def test_unsupported_param_type_is_rejected_with_actionable_message():
    class _Bad(Tool):
        async def execute(self, thing: object) -> ToolResult:  # type: ignore[override]
            return ToolResult(output='')

    with pytest.raises(Exception, match='thing'):
        _Bad().schema
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_tool.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.tool'`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/tool.py
"""工具基类与 schema 生成。

给模型看的与给代码用的必须分开（output / meta）：混在一起只能二选一——要么
模型上下文被结构化数据污染，要么工作流只能解析给人看的文本。

schema 三级回退的理由：工具描述质量直接决定模型是否用对它，因此必须允许
手写调优；但多数工具不需要那么精细，反射足够。
"""

from __future__ import annotations

import inspect

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from syndica.kernel.context import ArtifactRef, TrustLevel
from syndica.kernel.errors import ConfigError

__all__ = ['Tool', 'ToolResult']

_JSON_TYPES: dict[type, str] = {
    str: 'string',
    int: 'integer',
    float: 'number',
    bool: 'boolean',
    list: 'array',
    dict: 'object',
}


@dataclass
class ToolResult:
    output: str | list[Any]
    meta: dict[str, Any] = field(default_factory=dict[str, Any])
    artifacts: list[ArtifactRef] = field(default_factory=list[ArtifactRef])
    trust: TrustLevel = TrustLevel.TRUSTED


class Tool(ABC):
    __tool_name__: ClassVar[str | None] = None
    __tool_schema__: ClassVar[dict[str, Any] | None] = None
    __tool_model__: ClassVar[type[BaseModel] | None] = None

    @property
    def name(self) -> str:
        return self.__tool_name__ or type(self).__name__.lstrip('_')

    @property
    def description(self) -> str:
        return self.schema['description']

    @property
    def parameters(self) -> dict[str, Any]:
        return self.schema['parameters']

    @property
    def schema(self) -> dict[str, Any]:
        if self.__tool_schema__ is not None:
            return self.__tool_schema__
        if self.__tool_model__ is not None:
            return {
                'name': self.name,
                'description': (inspect.getdoc(type(self)) or '').strip(),
                'parameters': self.__tool_model__.model_json_schema(),
            }
        return self._schema_from_reflection()

    def _schema_from_reflection(self) -> dict[str, Any]:
        signature = inspect.signature(self.execute)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param_name, param in signature.parameters.items():
            if param_name == 'self' or param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            json_type = _JSON_TYPES.get(param.annotation)
            if json_type is None:
                raise ConfigError(
                    expected=f'参数 {param_name!r} 的标注是 {sorted(t.__name__ for t in _JSON_TYPES)} 之一',
                    received=f'收到标注 {param.annotation!r}',
                    next_action=(
                        f'把 {param_name!r} 改成受支持的类型，'
                        f'或给该工具定义 __tool_model__ / __tool_schema__'
                    ),
                )
            properties[param_name] = {'type': json_type}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        doc = (inspect.getdoc(self.execute) or '').strip()
        return {
            'name': self.name,
            'description': doc.split('\n\n')[0] if doc else '',
            'parameters': {
                'type': 'object',
                'properties': properties,
                'required': required,
            },
        }

    @abstractmethod
    async def execute(self, *args: Any, **kwargs: Any) -> ToolResult: ...

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        """默认不安全。判定从严：并发执行两个改同一文件的工具后果不可逆。"""
        return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_tool.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/tool.py tests/kernel/test_tool.py
git commit -m "feat(kernel): 工具基类与 schema 三级回退

ToolResult 的 output 与 meta 分开：混在一起只能二选一——要么模型上下文被
结构化数据污染（浪费 token 且干扰注意力），要么工作流只能解析给人看的文本
（脆弱，且会随提示词调整而崩）。

is_concurrency_safe 默认 False。代价不对称：并发执行两个修改同一文件的工具
后果不可逆，而串行只是慢一点。

反射生成遇到不支持的标注时报错而非静默降级——静默降级会让模型收到一个缺
参数的 schema，然后调用失败得莫名其妙。"
```

---

## Task 12: ToolExecutor（串行）

**Files:**
- Create: `src/syndica/kernel/tool_executor.py`
- Test: `tests/kernel/test_tool_executor.py`

**Interfaces:**
- Consumes: `Tool` / `ToolResult`（Task 11）、`ContextBlock` / `ToolCall`（Task 3）、`HooksCenter` / `Junction` / `HookResult`（Task 9）
- Produces: `ToolExecutor(tools, hooks)`：`execute_tool_calls(ctx, tool_calls) -> None`——把每个调用的结果作为 `role='tool'` block 追加进 ctx

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_tool_executor.py
from syndica.kernel.context import Context, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.hooks import HooksCenter, HookResult, Junction
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import ToolExecutor


class _Echo(Tool):
    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


class _Boom(Tool):
    async def execute(self) -> ToolResult:
        """总是抛非致命异常。"""
        raise RuntimeError('kaboom')


class _Fatal(Tool):
    async def execute(self) -> ToolResult:
        """总是抛致命异常。"""
        from syndica.kernel.errors import ToolFatalError

        raise ToolFatalError(
            expected='目标可写',
            received='路径不存在且父目录不可写',
            next_action='换一个可写路径',
        )


def _executor(**hooks: object) -> tuple[ToolExecutor, HooksCenter]:
    center = HooksCenter()
    tools = {'Echo': _Echo(), 'Boom': _Boom(), 'Fatal': _Fatal()}
    return ToolExecutor(tools=tools, hooks=center), center


async def test_result_becomes_tool_block_with_matching_id():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    block = ctx.current_traj[-1]
    assert block.role == 'tool'
    assert block.tool_call_id == 'c1'
    assert block.content == 'hi'


async def test_unknown_tool_yields_actionable_tool_block():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Nope', arguments='{}')])
    content = ctx.current_traj[-1].content
    assert isinstance(content, str)
    assert 'Nope' in content and 'Echo' in content


async def test_malformed_arguments_yield_actionable_tool_block():
    """模型经常产出非法 JSON。必须给它足以自我纠正的信息，而不是静默失败。"""
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Echo', arguments='{not json')])
    content = ctx.current_traj[-1].content
    assert isinstance(content, str)
    assert '{not json' in content


async def test_non_fatal_tool_exception_is_reported_not_raised():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Boom', arguments='{}')])
    content = ctx.current_traj[-1].content
    assert isinstance(content, str) and 'kaboom' in content


async def test_fatal_tool_error_propagates_to_agent_loop():
    """spec §10：致命错误必须上抛，Agent 需要知道这条路根本不通。

    转成 output 会让模型以为「再试一次也许能行」，于是它会一直试。
    """
    import pytest

    from syndica.kernel.errors import ToolFatalError

    executor, _ = _executor()
    ctx = Context()
    with pytest.raises(ToolFatalError):
        await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Fatal', arguments='{}')])


async def test_pre_tool_use_skip_substitutes_result():
    executor, center = _executor()

    async def block_it(**_: object) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='substituted'))

    center.register(Junction.PRE_TOOL_USE, block_it)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert ctx.current_traj[-1].content == 'substituted'


async def test_post_tool_use_can_rewrite_result():
    executor, center = _executor()

    async def rewrite(**kwargs: object) -> HookResult:
        return HookResult(modified={'result': ToolResult(output='rewritten')})

    center.register(Junction.POST_TOOL_USE, rewrite)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert ctx.current_traj[-1].content == 'rewritten'


async def test_untrusted_result_marks_the_tool_block():
    class _External(Tool):
        async def execute(self) -> ToolResult:
            """返回外部内容。"""
            return ToolResult(output='<html>', trust=TrustLevel.UNTRUSTED)

    center = HooksCenter()
    executor = ToolExecutor(tools={'External': _External()}, hooks=center)
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='External', arguments='{}')])
    assert ctx.current_traj[-1].trust is TrustLevel.UNTRUSTED


async def test_every_call_gets_exactly_one_block():
    """工具调用与结果必须配对，否则下一轮请求直接非法。"""
    executor, _ = _executor()
    ctx = Context()
    calls = [
        ToolCall(id='c1', name='Echo', arguments='{"text":"a"}'),
        ToolCall(id='c2', name='Nope', arguments='{}'),
        ToolCall(id='c3', name='Boom', arguments='{}'),
    ]
    await executor.execute_tool_calls(ctx, calls)
    ids = [b.tool_call_id for b in ctx.current_traj if b.role == 'tool']
    assert ids == ['c1', 'c2', 'c3']
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_tool_executor.py -v`
Expected: FAIL —— `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/tool_executor.py
"""工具执行。

本计划只做串行；分段并行随计划 2 补入。

两条不变式：
- 一次工具调用恰好产出一个 role='tool' block，配对断裂会让下一轮请求直接非法。
- ToolFatalError 上抛给 agent loop，其余异常转成 output 给模型（spec §10）。
  「什么算致命」由工具作者通过异常类型表达——executor 没有信息做这个判断。
"""

from __future__ import annotations

import json

from typing import Any

from syndica.kernel.context import Context, ContextBlock, ToolCall
from syndica.kernel.errors import ToolFatalError
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.tool import Tool, ToolResult

__all__ = ['ToolExecutor']


class ToolExecutor:
    def __init__(self, tools: dict[str, Tool], hooks: HooksCenter) -> None:
        self._tools = tools
        self._hooks = hooks

    async def execute_tool_calls(self, ctx: Context, tool_calls: list[ToolCall]) -> None:
        for call in tool_calls:
            result = await self._execute_one(call)
            ctx.append(
                ContextBlock(
                    role='tool',
                    content=result.output,
                    tool_call_id=call.id,
                    artifacts=list(result.artifacts),
                    trust=result.trust,
                    extra={'tool_meta': result.meta} if result.meta else {},
                )
            )

    async def _execute_one(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                output=(
                    f'期望: 工具名是已注册工具之一。'
                    f' 实际: 收到 {call.name!r}；可用工具: {sorted(self._tools)}。'
                    f' 下一步: 改用可用工具之一，不要发明工具名。'
                )
            )
        try:
            args: dict[str, Any] = json.loads(call.arguments) if call.arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolResult(
                output=(
                    f'期望: {call.name} 的 arguments 是合法 JSON 对象。'
                    f' 实际: 收到 {call.arguments!r}，解析失败于 {exc.msg}（位置 {exc.pos}）。'
                    f' 下一步: 重新输出合法 JSON；参数 schema: {tool.parameters}'
                )
            )

        pre = await self._hooks.trigger(
            Junction.PRE_TOOL_USE, tool_call=call, tool=tool, args=args
        )
        if pre.skip:
            substituted = pre.result
            return (
                substituted
                if isinstance(substituted, ToolResult)
                else ToolResult(output=str(substituted))
            )
        args = pre.get('args', args)

        try:
            result = await tool.execute(**args)
        except ToolFatalError:
            # 重试也不可能成功，上抛给 agent loop（spec §10）。转成 output 会让
            # 模型以为再试一次也许能行，于是它会一直试。
            raise
        except Exception as exc:
            result = ToolResult(
                output=(
                    f'期望: {call.name} 正常执行完成。'
                    f' 实际: 抛出 {type(exc).__name__}: {exc}。'
                    f' 下一步: 调整参数后重试，或换用其他方式达成同一目标。'
                )
            )

        post = await self._hooks.trigger(
            Junction.POST_TOOL_USE, tool_call=call, tool=tool, args=args, result=result
        )
        rewritten = post.get('result', result)
        return rewritten if isinstance(rewritten, ToolResult) else result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_tool_executor.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/tool_executor.py tests/kernel/test_tool_executor.py
git commit -m "feat(kernel): 工具执行，串行版

不变式：一次工具调用恰好产出一个 role='tool' block。配对断裂会让下一轮
请求直接非法，所以工具不存在、参数非法、执行抛异常这三种情况都必须产出
block 而不是跳过。

三种失败都按错误三件套写给模型看：模型是这些消息的第一读者，说清「可用
工具有哪些」「参数 schema 是什么」它就能自我纠正，说「invalid tool」它
只能重试同样的错误。"
```

---

## Task 13: BaseAgent turn 循环

**Files:**
- Create: `src/syndica/kernel/agent.py`
- Test: `tests/kernel/test_agent_turn.py`

**Interfaces:**
- Consumes: 全部前序任务
- Produces:
  - `AgentOptions(name, llm, tools, system_prompt, max_turns)`
  - `BaseAgent(options, hooks, events)`：`agent_id` / `run(query) -> Context` / `get_system_prompt() -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_agent_turn.py
import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import StopReason, ToolCall
from syndica.kernel.errors import LLMResponseError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter, HookResult, Junction
from syndica.kernel.llm import ContextOverflowError, ScriptedLLM, ScriptedTurn
from syndica.kernel.tool import Tool, ToolResult


class _Echo(Tool):
    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


def _agent(script: list[ScriptedTurn], **opts: object) -> tuple[BaseAgent, HooksCenter]:
    hooks = HooksCenter()
    options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM(script),
        tools={'Echo': _Echo()},
        system_prompt='You are a worker.',
        **opts,  # type: ignore[arg-type]
    )
    return BaseAgent(options, hooks=hooks, events=EventBus()), hooks


async def test_no_tool_calls_completes_immediately():
    agent, _ = _agent([ScriptedTurn(content='done')])
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert ctx.current_traj[-1].content == 'done'


async def test_tool_call_then_completion_takes_two_turns():
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]),
            ScriptedTurn(content='done'),
        ]
    )
    ctx = await agent.run('go')
    roles = [b.role for b in ctx.current_traj]
    assert roles == ['user', 'assistant', 'tool', 'assistant']


async def test_system_prompt_is_not_stored_in_trajectory():
    """SP 每轮重建并置于请求最前，但不进轨迹——否则压缩与持久化会重复携带它。"""
    agent, _ = _agent([ScriptedTurn(content='done')])
    ctx = await agent.run('go')
    assert all(b.role != 'system' for b in ctx.current_traj)


async def test_system_prompt_is_byte_identical_across_turns():
    """前缀缓存要求前缀逐轮字节一致；随轮变化的内容不得进 SP。"""
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]),
            ScriptedTurn(content='done'),
        ]
    )
    await agent.run('go')
    llm = agent.options.llm
    assert isinstance(llm, ScriptedLLM)
    first_sp = llm.calls[0][0]
    second_sp = llm.calls[1][0]
    assert first_sp.role == 'system' and second_sp.role == 'system'
    assert first_sp.content == second_sp.content


async def test_max_turns_stops_the_loop():
    agent, _ = _agent(
        [ScriptedTurn(tool_calls=[ToolCall(id=f'c{i}', name='Echo', arguments='{"text":"x"}')]) for i in range(5)],
        max_turns=2,
    )
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.MAX_TURNS


async def test_empty_assistant_block_is_treated_as_llm_failure():
    """既无 content 又无 tool_calls 的 block 不得入 context：它会污染此后所有请求的前缀。"""
    agent, _ = _agent([ScriptedTurn(content=None)])
    with pytest.raises(LLMResponseError):
        await agent.run('go')


async def test_llm_failure_hook_can_supply_substitute_response():
    agent, hooks = _agent([ScriptedTurn(raises=RuntimeError('flaky'))])

    async def substitute(**_: object) -> HookResult:
        from syndica.kernel.context import ContextBlock

        return HookResult(
            modified={'response': ContextBlock(role='assistant', content='recovered')}
        )

    hooks.register(Junction.LLM_FAILURE, substitute)
    ctx = await agent.run('go')
    assert ctx.current_traj[-1].content == 'recovered'


async def test_context_overflow_handled_by_hook_continues_next_turn():
    agent, hooks = _agent(
        [
            ScriptedTurn(raises=ContextOverflowError(
                expected='请求在窗口内', received='超窗', next_action='压缩后重试'
            )),
            ScriptedTurn(content='done'),
        ]
    )

    async def handled(**_: object) -> HookResult:
        return HookResult(skip=True)

    hooks.register(Junction.CONTEXT_OVERFLOW, handled)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_context_overflow_unhandled_propagates():
    agent, _ = _agent(
        [ScriptedTurn(raises=ContextOverflowError(
            expected='请求在窗口内', received='超窗', next_action='压缩后重试'
        ))]
    )
    with pytest.raises(ContextOverflowError):
        await agent.run('go')


async def test_pre_llm_request_hook_can_rewrite_messages():
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def inject(**kwargs: object) -> HookResult:
        from syndica.kernel.context import ContextBlock

        request = dict(kwargs['request'])  # type: ignore[arg-type]
        messages = list(request['messages'])
        messages.append(ContextBlock(role='user', content='injected'))
        request['messages'] = messages
        return HookResult(modified={'request': request})

    hooks.register(Junction.PRE_LLM_REQUEST, inject)
    await agent.run('go')
    llm = agent.options.llm
    assert isinstance(llm, ScriptedLLM)
    assert llm.calls[0][-1].content == 'injected'


async def test_turn_events_are_emitted():
    hooks = HooksCenter()
    bus = EventBus()
    options = AgentOptions(
        name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')]), tools={},
        system_prompt='SP',
    )
    agent = BaseAgent(options, hooks=hooks, events=bus)
    kinds: list[str] = []

    async def collect() -> None:
        async for event in bus.subscribe():
            kinds.append(event.kind)
            if event.kind == 'agent_end':
                break

    import asyncio

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await agent.run('go')
    await asyncio.wait_for(task, timeout=1.0)
    assert kinds[0] == 'agent_start'
    assert 'turn_start' in kinds and 'turn_end' in kinds
    assert kinds[-1] == 'agent_end'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_agent_turn.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.agent'`

- [ ] **Step 3: 写实现**

```python
# src/syndica/kernel/agent.py
"""BaseAgent：单轮 turn 循环。

Agent 不管理自己的生命周期：外层由 Runtime 驱动，本类只跑「LLM + 工具」
循环（spec §4）。System Prompt 每轮重建并置于请求最前，但不进轨迹——
它是请求的前缀而非对话的一部分。
"""

from __future__ import annotations

import itertools
import uuid

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from syndica.kernel.context import Context, ContextBlock, StopReason
from syndica.kernel.errors import LLMResponseError
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.llm import LLM, ContextOverflowError
from syndica.kernel.tool import Tool
from syndica.kernel.tool_executor import ToolExecutor

__all__ = ['AgentOptions', 'BaseAgent']


@dataclass
class AgentOptions:
    name: str
    llm: LLM
    tools: dict[str, Tool] = field(default_factory=dict[str, Tool])
    system_prompt: str = ''
    max_turns: int | None = 50


class BaseAgent:
    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        self.options = options
        self.agent_id = uuid.uuid4().hex[:12]
        self._hooks = hooks
        self._events = events
        self._executor = ToolExecutor(tools=options.tools, hooks=hooks)
        self._current_turn = 0

    def get_system_prompt(self) -> str:
        """必须逐轮返回字节一致的结果：SP 变一个字节，该会话此后所有请求缓存全失效。"""
        return self.options.system_prompt

    async def run(self, query: str) -> Context:
        ctx = Context()
        ctx.append(ContextBlock(role='user', content=query))
        await self._emit('agent_start', {})
        await self._hooks.trigger(Junction.AGENT_START, ctx=ctx)
        try:
            await self._run_turns(ctx)
        except Exception:
            ctx.stop_reason = StopReason.ERROR
            await self._hooks.trigger(Junction.AGENT_ERROR, ctx=ctx)
            await self._emit('agent_error', {})
            raise
        finally:
            if ctx.stop_reason is not StopReason.ERROR:
                await self._hooks.trigger(Junction.AGENT_END, ctx=ctx)
                await self._emit('agent_end', {'stop_reason': ctx.stop_reason})
        return ctx

    async def _run_turns(self, ctx: Context) -> None:
        tool_defs = [tool.schema for tool in self.options.tools.values()]
        max_turns = self.options.max_turns
        turns: Iterable[int] = (
            itertools.count(self._current_turn + 1)
            if max_turns is None
            else range(self._current_turn + 1, max_turns + 1)
        )
        for turn in turns:
            self._current_turn = turn
            await self._emit('turn_start', {'turn': turn})
            await self._hooks.trigger(Junction.TURN_START, ctx=ctx, turn=turn)

            response = await self._one_llm_call(ctx, tool_defs)
            if response is None:
                # 超窗已被插件处理，本轮不产出 assistant block，直接进下一轮。
                await self._hooks.trigger(Junction.TURN_END, ctx=ctx, turn=turn, message=None)
                await self._emit('turn_end', {'turn': turn})
                continue

            ctx.append(response)
            if not response.tool_calls:
                ctx.stop_reason = StopReason.COMPLETED
                await self._hooks.trigger(Junction.TURN_END, ctx=ctx, turn=turn, message=response)
                await self._emit('turn_end', {'turn': turn})
                return

            await self._executor.execute_tool_calls(ctx, response.tool_calls)
            await self._hooks.trigger(Junction.TURN_END, ctx=ctx, turn=turn, message=response)
            await self._emit('turn_end', {'turn': turn})
        else:
            ctx.stop_reason = StopReason.MAX_TURNS

    async def _one_llm_call(
        self, ctx: Context, tool_defs: list[dict[str, Any]]
    ) -> ContextBlock | None:
        system_block = ContextBlock(role='system', content=self.get_system_prompt())
        request: dict[str, Any] = {
            'messages': [system_block, *ctx.current_traj],
            'tools': tool_defs,
        }
        hooked = await self._hooks.trigger(Junction.PRE_LLM_REQUEST, ctx=ctx, request=request)
        request = hooked.get('request', request)
        messages = request['messages']
        kwargs = {k: v for k, v in request.items() if k != 'messages'}

        try:
            response = await self.options.llm.create(messages, **kwargs)
        except ContextOverflowError as exc:
            outcome = await self._hooks.trigger(
                Junction.CONTEXT_OVERFLOW, ctx=ctx, request=request, error=exc
            )
            if outcome.skip:
                return None
            raise
        except Exception as exc:
            substitute = await self._handle_llm_failure(ctx, request, exc)
            if substitute is None:
                raise
            response = substitute

        post = await self._hooks.trigger(
            Junction.POST_LLM_RESPONSE, ctx=ctx, request=request, response=response
        )
        response = post.get('response', response)

        if response.role == 'assistant' and not response.content and not response.tool_calls:
            empty = LLMResponseError(
                expected='assistant 响应至少含 content 或 tool_calls 之一',
                received=f'provider={self.options.llm!r} 返回了两者皆空的响应',
                next_action='检查 provider 与模型配置；若为偶发，装一个重试 Module 处理',
            )
            substitute = await self._handle_llm_failure(ctx, request, empty)
            if substitute is None:
                raise empty
            response = substitute
        return response

    async def _handle_llm_failure(
        self, ctx: Context, request: dict[str, Any], error: BaseException
    ) -> ContextBlock | None:
        outcome = await self._hooks.trigger(
            Junction.LLM_FAILURE, ctx=ctx, request=request, error=error
        )
        substitute = outcome.get('response')
        if isinstance(substitute, ContextBlock):
            return substitute
        for block in reversed(ctx.current_traj):
            if block.role in ('user', 'tool'):
                block.extra['llm_call_failed'] = True
                break
        return None

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        await self._events.emit(Event(kind=kind, agent_id=self.agent_id, payload=payload))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_agent_turn.py -v`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/agent.py tests/kernel/test_agent_turn.py
git commit -m "feat(kernel): BaseAgent turn 循环

三处是踩过坑的地方，实现时按原样保留：

1. LLM 异常必须分三类，不能合并。超窗触发 CONTEXT_OVERFLOW 让压缩插件
   处理并重试下一轮；取消直接透传绝不当失败；其余触发 LLM_FAILURE，插件
   可返回替身响应（重试与故障转移在此实现）
2. 空 assistant block 绝不入 context。既无 content 又无 tool_calls 的
   block 会污染此后所有请求的前缀，故障表现为后续全部请求异常，排查成本
   极高
3. System Prompt 每轮重建并置于请求最前，但不进轨迹。它是请求前缀而非
   对话内容，进轨迹会让压缩与持久化重复携带它

max_turns 约束整个任务而非单个周期：轮次计数跨周期不重置。"
```

---

## Task 14: Runtime 最小闭环与端到端验证

**Files:**
- Create: `src/syndica/kernel/runtime.py`
- Modify: `src/syndica/kernel/__init__.py`
- Test: `tests/kernel/test_runtime.py`
- Test: `tests/integration/test_single_agent_turn.py`

**Interfaces:**
- Consumes: 全部前序任务
- Produces:
  - `Runtime(registry=None, broker=None)`：`.events` / `.hooks` / `spawn(agent_factory, *, query, exit_mode)` / `wait(agent_id) -> Context` / `send(agent_id, content, *, trigger, activate) -> DeliveryResult` / `find(agent_id) -> AgentInfo | None` / `shutdown()`
  - `syndica.kernel` 顶层导出

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_runtime.py
from syndica.kernel.agent_types import AgentStatus, ExitMode
from syndica.kernel.context import StopReason, ToolCall
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult


class _Echo(Tool):
    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


def _factory(script: list[ScriptedTurn], name: str = 'Worker'):
    from syndica.kernel.agent import AgentOptions, BaseAgent

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name=name, llm=ScriptedLLM(script), tools={'Echo': _Echo()}, system_prompt='SP'
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def test_spawn_registers_agent_and_returns_id():
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory([ScriptedTurn(content='done')]), query='go')
    info = await runtime.find(agent_id)
    assert info is not None
    assert info.name == 'Worker'
    await runtime.shutdown()


async def test_wait_returns_completed_context():
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory([ScriptedTurn(content='done')]), query='go')
    ctx = await runtime.wait(agent_id)
    assert ctx.stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_status_returns_to_idle_after_completion_when_exit_mode_idle():
    runtime = Runtime()
    agent_id = await runtime.spawn(
        _factory([ScriptedTurn(content='done')]), query='go', exit_mode=ExitMode.IDLE
    )
    await runtime.wait(agent_id)
    info = await runtime.find(agent_id)
    assert info is not None and info.status is AgentStatus.IDLE
    await runtime.shutdown()


async def test_zombie_exit_mode_keeps_agent_addressable():
    """ZOMBIE 保留在注册表因此仍可寻址——一条消息就能让它继续干。"""
    runtime = Runtime()
    agent_id = await runtime.spawn(
        _factory([ScriptedTurn(content='done')]), query='go', exit_mode=ExitMode.ZOMBIE
    )
    await runtime.wait(agent_id)
    info = await runtime.find(agent_id)
    assert info is not None and info.status is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_find_on_unknown_agent_returns_none_without_activating():
    runtime = Runtime()
    assert await runtime.find('nope') is None
    await runtime.shutdown()


async def test_wait_on_unknown_agent_raises_actionable_error():
    """裸 KeyError 违反错误三件套，调用方看不出该怎么办。"""
    import pytest

    from syndica.kernel.errors import ConfigError

    runtime = Runtime()
    with pytest.raises(ConfigError) as info:
        await runtime.wait('nope')
    assert '下一步' in str(info.value)
    await runtime.shutdown()


async def test_send_to_unknown_agent_returns_structured_failure():
    """投递失败返回结构化结果而不抛异常，调用方才能识别可重试的瞬时错误。"""
    runtime = Runtime()
    result = await runtime.send('nope', 'hello')
    assert result.ok is False
    assert result.reason is not None and '下一步' in result.reason
    await runtime.shutdown()


async def test_tool_using_agent_completes_through_runtime():
    runtime = Runtime()
    agent_id = await runtime.spawn(
        _factory(
            [
                ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]),
                ScriptedTurn(content='done'),
            ]
        ),
        query='go',
    )
    ctx = await runtime.wait(agent_id)
    assert [b.role for b in ctx.current_traj] == ['user', 'assistant', 'tool', 'assistant']
    await runtime.shutdown()
```

```python
# tests/integration/test_single_agent_turn.py
"""端到端：本计划的交付判据。"""

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
)


class Greeter(Tool):
    async def execute(self, name: str) -> ToolResult:
        """向 name 打招呼。"""
        return ToolResult(output=f'Hello, {name}!', meta={'greeted': name})


async def test_agent_calls_tool_then_answers():
    runtime = Runtime()

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Greeter',
                llm=ScriptedLLM(
                    [
                        ScriptedTurn(
                            tool_calls=[
                                ToolCall(id='c1', name='Greeter', arguments='{"name":"Loren"}')
                            ]
                        ),
                        ScriptedTurn(content='I greeted Loren.'),
                    ]
                ),
                tools={'Greeter': Greeter()},
                system_prompt='You greet people.',
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='Please greet Loren')
    ctx = await runtime.wait(agent_id)

    assert ctx.stop_reason is StopReason.COMPLETED
    tool_block = next(b for b in ctx.current_traj if b.role == 'tool')
    assert tool_block.content == 'Hello, Loren!'
    assert tool_block.extra['tool_meta'] == {'greeted': 'Loren'}
    assert ctx.current_traj[-1].content == 'I greeted Loren.'

    await runtime.shutdown()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_runtime.py tests/integration -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.runtime'`

- [ ] **Step 3: 写 Runtime**

```python
# src/syndica/kernel/runtime.py
"""Runtime：Agent 生命周期。

按操作系统进程模型设计：Runtime 类比 init 系统，Agent 类比进程。
Agent 不管理自己的生命周期——外层由本类驱动，内层只跑 LLM + 工具循环。

本计划只做最小闭环（spawn / wait / send / find / shutdown）。事件循环、
kill / pause / abort、按需激活随计划 2 补入。
"""

from __future__ import annotations

import asyncio

from collections.abc import Callable
from typing import Any

from syndica.kernel.agent import BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import Context
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.local_table import LocalAgentTable
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MessageBroker
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker

__all__ = ['AgentFactory', 'Runtime']

AgentFactory = Callable[[HooksCenter, EventBus], BaseAgent]


class Runtime:
    def __init__(
        self,
        *,
        registry: AgentRegistry | None = None,
        broker: MessageBroker | None = None,
    ) -> None:
        self.registry: AgentRegistry = registry or InMemoryAgentRegistry()
        self.broker: MessageBroker = broker or InMemoryMessageBroker()
        self.events = EventBus()
        self.hooks = HooksCenter()
        self._table: LocalAgentTable[BaseAgent] = LocalAgentTable()
        self._contexts: dict[str, Context] = {}

    async def spawn(
        self,
        factory: AgentFactory,
        *,
        query: str,
        exit_mode: ExitMode = ExitMode.ZOMBIE,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        agent = factory(self.hooks, self.events)
        agent_id = agent.agent_id
        await self.registry.register(
            AgentInfo(
                agent_id=agent_id,
                name=agent.options.name,
                agent_type=type(agent).__name__,
                parent_id=parent_id,
                metadata=metadata or {},
            )
        )
        await self.broker.ensure_queue(agent_id)
        self._table.put(agent_id, agent)
        task = asyncio.create_task(
            self._drive(agent_id, agent, query, exit_mode), name=f'agent:{agent_id}'
        )
        self._table.set_task(agent_id, task)
        return agent_id

    async def _drive(
        self, agent_id: str, agent: BaseAgent, query: str, exit_mode: ExitMode
    ) -> None:
        await self.registry.compare_and_set_status(
            agent_id, expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        )
        try:
            self._contexts[agent_id] = await agent.run(query)
        finally:
            terminal = {
                ExitMode.IDLE: AgentStatus.IDLE,
                ExitMode.ZOMBIE: AgentStatus.ZOMBIE,
                ExitMode.EXIT: AgentStatus.ZOMBIE,
            }[exit_mode]
            await self.registry.compare_and_set_status(
                agent_id, expected=AgentStatus.RUNNING, new=terminal
            )
            if exit_mode is ExitMode.EXIT:
                await self.broker.remove_queue(agent_id)
                await self.registry.unregister(agent_id)
                self._table.pop(agent_id)

    async def wait(self, agent_id: str) -> Context:
        """类比 waitpid。"""
        task = self._table.get_task(agent_id)
        if task is not None:
            await task
        context = self._contexts.get(agent_id)
        if context is None:
            raise ConfigError(
                expected=f'agent {agent_id!r} 曾经运行过并留下了 Context',
                received=(
                    '本进程没有该 agent 的运行记录'
                    f'（已知: {sorted(self._contexts)}）'
                ),
                next_action=(
                    '确认 agent_id 是 spawn 的返回值；'
                    '若该 agent 由其他进程持有，本进程无法 wait 它'
                ),
            )
        return context

    async def send(
        self,
        agent_id: str,
        content: str,
        *,
        trigger: Trigger = Trigger.PENDING,
        activate: bool = True,
    ) -> DeliveryResult:
        if not activate and await self.registry.get(agent_id) is None:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 已注册。'
                    f' 实际: 注册表中不存在该条目，且 activate=False 禁止激活。'
                    f' 下一步: 先 spawn 该 agent，或改用 activate=True'
                ),
            )
        return await self.broker.publish(
            agent_id,
            Message(sender='runtime', receiver=agent_id, content=content, trigger=trigger),
        )

    async def find(self, agent_id: str) -> AgentInfo | None:
        """只读路径，绝不激活任何 Agent。"""
        return await self.registry.get(agent_id)

    async def shutdown(self) -> None:
        for agent_id in self._table.agent_ids():
            task = self._table.get_task(agent_id)
            if task is not None and not task.done():
                task.cancel()
        await self.events.close()
```

- [ ] **Step 4: 写顶层导出**

```python
# src/syndica/kernel/__init__.py
"""Kernel: runtime, agent, tools, hooks, protocols.

零重依赖：只允许标准库 + pydantic + 一个 HTTP 客户端。
不读环境变量：路径 / 凭证 / 开关由宿主注入。
"""

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import (
    ArtifactRef,
    Context,
    ContextBlock,
    StopReason,
    ToolCall,
    TrustLevel,
)
from syndica.kernel.errors import (
    ConfigError,
    ContractViolation,
    LLMResponseError,
    SyndicaError,
    ToolFatalError,
)
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLM, ContextOverflowError, LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MessageBroker
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult

__all__ = [
    'LLM',
    'AgentFactory',
    'AgentInfo',
    'AgentOptions',
    'AgentRegistry',
    'AgentStatus',
    'ArtifactRef',
    'BaseAgent',
    'ConfigError',
    'Context',
    'ContextBlock',
    'ContextOverflowError',
    'ContractViolation',
    'DeliveryResult',
    'Event',
    'EventBus',
    'ExitMode',
    'HookResult',
    'HooksCenter',
    'InMemoryAgentRegistry',
    'InMemoryMessageBroker',
    'Junction',
    'LLMConfig',
    'LLMResponseError',
    'Message',
    'MessageBroker',
    'Runtime',
    'ScriptedLLM',
    'ScriptedTurn',
    'StopReason',
    'SyndicaError',
    'Tool',
    'ToolCall',
    'ToolFatalError',
    'ToolResult',
    'Trigger',
    'TrustLevel',
]
```

- [ ] **Step 5: 运行全部测试**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run python scripts/check_deps.py`
Expected: 全部 passed；lint、format、类型检查与依赖方向校验均通过

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/runtime.py src/syndica/kernel/__init__.py tests/kernel/test_runtime.py tests/integration/test_single_agent_turn.py
git commit -m "feat(kernel): Runtime 最小闭环，单 Agent 一轮跑通

至此计划 1 的交付判据达成：创建一个 Agent、给它一句话，它调 LLM、执行工具、
返回结果，且状态转换、事件流、契约测试全部就位。

两处刻意的设计：
- find() 明文承诺绝不激活任何 Agent。只读路径与激活路径必须分离，否则查询
  类工具无法把「不存在」和「存在但未运行」分开回答
- 状态转换全部走 compare_and_set_status。单进程下直接赋值也能跑，但那样
  写出来的代码换成 Redis 实现就会静默丢转换，而这类 bug 只在生产暴露

事件循环、kill / pause / abort、按需激活留给计划 2。"
```

---

## 预检修正记录

执行前的冲突预检发现四处问题，前三处已修进本计划：

| # | 问题 | 修法 |
| --- | --- | --- |
| 1 | Task 12 用 `except Exception` 捕获全部工具异常并转成 output，**与 spec §10「致命错误抛出交给 Agent 循环」直接矛盾**。致命错误被吞掉后，模型会以为再试一次也许能行，于是一直试 | 新增 `ToolFatalError`（Task 2）：继承它的异常上抛，其余转 output。**「什么算致命」由工具作者通过异常类型表达**——executor 没有信息做这个判断 |
| 2 | `Runtime.wait()` 对未知 agent 抛裸 `KeyError`，违反 Global Constraints 的错误三件套 | 改为抛 `ConfigError`，并列出本进程已知的 agent |
| 3 | `EventBus._closed` 赋值两次、从不读取。它还暗示一个未定行为：`close()` 之后 `emit` 与 `subscribe` 该怎么办 | `emit` 关闭后静默丢弃（可观测性不得拖垮主流程）；`subscribe` 关闭后立即结束，否则迟到的订阅者会永久挂在空队列上 |
| 4 | `take_by_correlation` 无条件 clear 共享 event，可能吞掉并发 `wait_for_messages` 的通知 | **未修**。串行单测不会暴露，但这是通知组件自身的正确性问题。记为 Minor 交最终整枝 review 裁决 |

---

## 自检

**1. 规格覆盖** —— 本计划对应 spec 里程碑 1 的以下部分：

| spec 章节 | 覆盖情况 |
| --- | --- |
| §3.2 依赖规则 | Task 1（机器校验，含反向测试） |
| §4.1 六态状态机 + ExitMode | Task 4（类型）、Task 14（转换） |
| §4.2 Runtime 公开 API | Task 14 覆盖 `spawn` / `wait` / `send` / `find`；`kill` / `pause` / `resume` / `abort` / `despawn` / `ensure_active` / `wait_for` → 计划 2 |
| §4.3 注册表与实例表分离、租约字段 | Task 4、Task 5、Task 7 |
| §4.4 Registry / Broker Protocol + CAS | Task 5、Task 6 |
| §4.5 消息语义（Trigger 双时机） | Task 6 定义类型；DEFERRED 的消费时机 → 计划 2 |
| §5.1 turn 循环、异常三级分类、空响应守卫 | Task 13 |
| §5.2 前缀缓存（SP 逐轮字节一致） | Task 13 有专项测试；漂移检测 → 计划 5 |
| §5.3 扩展装配、§5.4 切点、`HookResult` | Task 9（本计划所需切点子集） |
| §5.6 Tool 系统、schema 三级回退 | Task 11、Task 12（并发分段 → 计划 2） |
| §5.7 LLM 抽象、`derive` | Task 10（重试 Module → 计划 5） |
| §7.2 `ArtifactRef`、§7.3 上下文字段 | Task 3（字段就位，行为 → 里程碑 7） |
| §9.1 事件流进程内可订阅 | Task 8 |
| §12.2 不可信标记 | Task 3、Task 12（标记就位与传递，强制处置 → 里程碑 5） |
| §13.3 契约测试对外发布 | Task 5、Task 6 |
| Global Constraints 错误三件套 | Task 2 强制，各任务的错误消息遵循 |

**明确不在本计划**：`SessionStore` / `ArtifactStore` 的实现与契约（里程碑 6 / 7）、Runtime Module（计划 5）、声明式 Agent 与拓扑（计划 3）、Workspace / Portal（计划 4）、CLI（计划 6）、L2 声明式场景层（计划 6，本计划提供其基础 `ScriptedLLM`）。

**2. 占位符扫描** —— 无 TBD / TODO；每个代码步骤都给出完整可运行代码；无"参照 Task N"式引用。

**3. 类型一致性核对**

- `AgentInfo` 字段名在 Task 4 定义，Task 5 契约与实现、Task 14 构造处一致。
- `compare_and_set_status` 的关键字参数形式（`expected=` / `new=`）在 Protocol、内存实现、契约测试、Runtime 四处一致。
- `ToolResult` 四字段（`output` / `meta` / `artifacts` / `trust`）在 Task 11 定义，Task 12 消费、Task 14 集成测试断言一致。
- `HookResult.get(key, default)` 在 Task 9 定义，Task 12 与 Task 13 均按此签名调用。
- `ScriptedTurn(content, tool_calls, raises)` 在 Task 10 定义，Task 13、Task 14 使用一致。
- `Junction` 成员名（`PRE_LLM_REQUEST` 等）在 Task 9 定义，Task 12、Task 13 引用一致。
- `EventBus.emit` / `subscribe` / `close` 在 Task 8 定义，Task 13、Task 14 使用一致。
