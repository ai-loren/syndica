"""拓扑槽（spec §4.7）。装配与启动不在本模块。

槽只增强角色清单，不替换它。没有 `extra_plugins`：插件只从 `Runtime(plugins=)` 进入。
`keyed` / `on-demand` 不能 `boot='eager'`——没有键或调用方，启动时无从实例化。
`metadata` 不得含 `artifact_kinds`：occupy 会把清单上的产物种类并进 metadata，键冲突必须当场失败。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from syndica.kernel.agent_types import ExitMode
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.errors import ConfigError
from syndica.kernel.isolation import ISOLATIONS

__all__ = ['AgentSlot', 'Topology']

_CARDINALITIES: tuple[str, ...] = ('singleton', 'keyed', 'on-demand')
_BOOTS: tuple[str, ...] = ('eager', 'lazy', 'manual')
# occupy 把清单的 artifact_kinds 并进实例 metadata。槽上先占了这个键，合并只能静默覆盖或崩。
_RESERVED_METADATA_KEYS: frozenset[str] = frozenset({'artifact_kinds'})


def _empty_metadata() -> Mapping[str, object]:
    return MappingProxyType({})


@dataclass(frozen=True)
class AgentSlot:
    """一个拓扑槽。字段顺序是公开契约，新字段只能追加。

    `extra_tools` 是要并上的工具名，不是替换清单里已有的名字。名字是否存在留到 occupy。

    `isolation` 是这个槽在工作区里的位置（结构，不是行为）：`worktree` / `snapshot` 让占着它的
    Agent 每个周期在共享工作区此刻的一份拷贝里干活，周期结束时合并回去，冲突在下一个周期开头告诉
    它。每个周期拷一次：工作区大时这份开销按周期付。
    """

    name: str
    agent: str
    is_root: bool = False
    cardinality: str = 'on-demand'
    key_field: str | None = None
    boot: str = 'lazy'
    exit_mode: ExitMode = ExitMode.ZOMBIE
    extra_tools: tuple[str, ...] = ()
    budget: Mapping[str, int | None] | None = None
    metadata: Mapping[str, object] = field(default_factory=_empty_metadata)
    isolation: str = 'shared'

    def __post_init__(self) -> None:
        _nonblank_str(self.name, 'name')
        _nonblank_str(self.agent, 'agent')
        _require_bool(self.is_root, 'is_root')
        _require_choice(self.cardinality, 'cardinality', _CARDINALITIES)
        _require_choice(self.boot, 'boot', _BOOTS)
        _check_key_field(self.cardinality, self.key_field)
        _reject_eager_without_singleton(self.cardinality, self.boot)
        _require_exit_mode(self.exit_mode)
        _require_choice(self.isolation, 'isolation', ISOLATIONS)
        object.__setattr__(self, 'extra_tools', _extra_tools(self.extra_tools))
        object.__setattr__(self, 'budget', _budget(self.budget))
        object.__setattr__(self, 'metadata', _metadata(self.metadata))


@dataclass(frozen=True)
class Topology:
    """一组槽。槽名唯一，最多一个 root；零个 root 合法。

    槽引用的角色名不在构造时查——那时还没有目录。`validate(catalog)` 一次列出全部缺失名。
    """

    slots: tuple[AgentSlot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'slots', _slots(self.slots))
        _reject_duplicate_names(self.slots)
        _reject_extra_roots(self.slots)

    def validate(self, catalog: object) -> None:
        """每个槽的 `agent` 都必须是目录里的角色名。缺失名全部列出，不要停在第一个。也收
        「角色名 → 已冻结的工厂」的映射，按它的键校验（回放与场景交冻结工厂装配时没有目录）。"""
        if isinstance(catalog, Mapping):
            names = [
                name for name in cast('Mapping[object, object]', catalog) if isinstance(name, str)
            ]
        elif isinstance(catalog, AgentCatalog):
            names = list(catalog.names())
        else:
            raise ConfigError(
                expected='catalog 是 AgentCatalog，或「角色名 → 已冻结的工厂」的映射',
                received=f'收到 {type(catalog).__name__} 类型的 {catalog!r}',
                next_action='传入 AgentCatalog；先 load_mapping / register_class 再 validate',
            )
        known = set(names)
        missing: list[str] = []
        for slot in self.slots:
            if slot.agent not in known and slot.agent not in missing:
                missing.append(slot.agent)
        if missing:
            raise ConfigError(
                expected='每个槽的 agent 都是目录里已有的角色名',
                received=(f'目录现有 {names!r}，缺少 {missing!r}'),
                next_action=(
                    '先 load_mapping / register_class / register_builtin 登记这些角色，'
                    '或改槽的 agent 字段。names() 给出第一次见到的顺序'
                ),
            )


def _nonblank_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            expected=f'{field_name} 是非空白字符串',
            received=f'{field_name}={value!r}，类型是 {type(value).__name__}',
            next_action=f'把 {field_name} 改成非空白 str',
        )
    if not value.strip():
        raise ConfigError(
            expected=f'{field_name} 是非空白字符串',
            received=f'{field_name}={value!r}',
            next_action=f'把 {field_name} 改成非空白 str；空白名字让 Delegate 寻址落空',
        )
    return value


def _budget(value: object) -> Mapping[str, int | None] | None:
    """与清单的 budget 同一约束：只有 turns。槽上的 turns 只收紧实例 max_turns，不改角色清单。"""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Mapping):
        raise ConfigError(
            expected='budget 是只含 turns 键的映射，或省略（则 max_turns 保持 50）',
            received=f'budget={value!r}，类型是 {type(value).__name__}',
            next_action='写成 {turns: 40}，或 turns: null 表示不限轮次',
        )
    budget = cast(Mapping[object, object], value)
    extra = [key for key in budget if key != 'turns']
    if extra:
        raise ConfigError(
            expected='budget 只允许键 turns',
            received=f'budget 里还有 {sorted(str(key) for key in extra)!r}',
            next_action=(
                '删掉这些键。槽能映射的只有 turns → 实例的 max_turns；'
                '其它预算字段还没有读者，写了等于改了却不生效'
            ),
        )
    if 'turns' not in budget:
        raise ConfigError(
            expected='budget 若出现，必须有 turns',
            received=f'budget={dict(budget)!r}，没有 turns',
            next_action='写成 {turns: 40}，或不写 budget 以使用默认 50 轮',
        )
    turns = budget['turns']
    if turns is None:
        return MappingProxyType({'turns': None})
    if isinstance(turns, bool) or not isinstance(turns, int):
        raise ConfigError(
            expected='budget.turns 是整数或 null（不限轮次）',
            received=(
                f'turns={turns!r}，类型是 {type(turns).__name__}'
                + ('：bool 是 int 的子类，YAML yes 会变成 1 轮' if isinstance(turns, bool) else '')
            ),
            next_action='给 budget.turns 传整数轮次，或 null；不要传布尔值或浮点',
        )
    if turns < 0:
        raise ConfigError(
            expected='budget.turns 是 >= 0 的整数或 null',
            received=f'turns={turns!r}',
            next_action='传非负整数，或 null 表示不限',
        )
    return MappingProxyType({'turns': turns})


def _require_bool(value: object, field_name: str) -> None:
    if isinstance(value, bool):
        return
    raise ConfigError(
        expected=f'{field_name} 是 bool',
        received=(
            f'{field_name}={value!r}，类型是 {type(value).__name__}'
            + ('：bool 是 int 的子类，YAML yes 会变成 1' if isinstance(value, int) else '')
        ),
        next_action='传 True 或 False；不要传 1 / 0',
    )


def _require_choice(value: object, field_name: str, allowed: tuple[str, ...]) -> None:
    if isinstance(value, str) and value in allowed:
        return
    raise ConfigError(
        expected=f'{field_name} 是 {list(allowed)!r} 之一',
        received=f'{field_name}={value!r}，类型是 {type(value).__name__}',
        next_action=f'改成 {list(allowed)!r} 里的一个字符串',
    )


def _check_key_field(cardinality: str, key_field: object) -> None:
    if cardinality == 'keyed':
        _nonblank_str(key_field, 'key_field')
        return
    if key_field is not None:
        raise ConfigError(
            expected=f'cardinality={cardinality!r} 时 key_field 是 None',
            received=f'key_field={key_field!r}',
            next_action=(
                '删掉 key_field。只有 keyed 槽才按这个字段分实例；'
                'singleton 与 on-demand 写了键也不会被读'
            ),
        )


def _reject_eager_without_singleton(cardinality: str, boot: str) -> None:
    if boot != 'eager' or cardinality == 'singleton':
        return
    raise ConfigError(
        expected='eager 只允许 cardinality=singleton：启动时没有键，也没有调用方',
        received=f'cardinality={cardinality!r}，boot={boot!r}',
        next_action=(
            "改成 boot='lazy' 或 boot='manual'，或把 cardinality 改成 'singleton'。"
            'keyed 要等 key 出现，on-demand 要等调用方'
        ),
    )


def _require_exit_mode(value: object) -> None:
    if isinstance(value, ExitMode):
        return
    allowed = [member.name for member in ExitMode]
    raise ConfigError(
        expected=f'exit_mode 是 ExitMode 的成员（{allowed!r}）',
        received=f'exit_mode={value!r}，类型是 {type(value).__name__}',
        next_action='传入 ExitMode.ZOMBIE / ExitMode.IDLE / ExitMode.EXIT，不要传字符串',
    )


def _extra_tools(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ConfigError(
            expected='extra_tools 是字符串名字的列表或元组',
            received=(
                f'extra_tools={value!r}：str / bytes 也是 Sequence，按序列收下会把 '
                f'{value!r} 拆成单个字符'
            ),
            next_action='写成 ["Read", "Grep"] 这样的名字列表',
        )
    if isinstance(value, Mapping):
        keys = list(cast(Mapping[object, object], value))
        raise ConfigError(
            expected='extra_tools 是名字列表，不是「名字 → Tool」的映射',
            received=f'extra_tools 是 {type(cast(object, value)).__name__}，键为 {keys!r}',
            next_action='写成名字列表。工具对象在 occupy 时按目录解析，不要直接塞进槽',
        )
    if not isinstance(value, Sequence):
        raise ConfigError(
            expected='extra_tools 是字符串名字的列表或元组',
            received=f'extra_tools={value!r}，类型是 {type(value).__name__}',
            next_action='写成 ["Read", "Grep"]，或省略（默认空元组）',
        )
    items = cast(Sequence[object], value)
    offenders = [item for item in items if not isinstance(item, str) or not item.strip()]
    if offenders:
        raise ConfigError(
            expected='extra_tools 的每一项都是非空白字符串',
            received=f'不合格的项 {offenders!r}',
            next_action='每一项改成非空白的工具名；空字符串和数字都不是名字',
        )
    return tuple(cast(str, item) for item in items)


def _metadata(value: object) -> Mapping[str, object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Mapping):
        raise ConfigError(
            expected='metadata 是字符串键的映射',
            received=f'metadata={value!r}，类型是 {type(value).__name__}',
            next_action='写成 {"lane": "a"} 这样的映射，或省略',
        )
    mapping = cast(Mapping[object, object], value)
    bad_keys = [key for key in mapping if not isinstance(key, str)]
    if bad_keys:
        raise ConfigError(
            expected='metadata 的键都是 str',
            received=f'非字符串键 {bad_keys!r}',
            next_action='把键改成字符串。整数键与字符串键在合并时对不上',
        )
    reserved = [key for key in mapping if isinstance(key, str) and key in _RESERVED_METADATA_KEYS]
    if reserved:
        raise ConfigError(
            expected='metadata 不得占用内核合并 artifact_kinds 时要写的键',
            received=f'占用了 {reserved!r}',
            next_action=(
                '删掉 artifact_kinds。角色产物种类写在清单的 artifact_kinds 上，'
                'occupy 时再并进实例 metadata'
            ),
        )
    try:
        copied = copy.deepcopy(dict(cast(Mapping[str, object], mapping)))
    except Exception as exc:
        raise ConfigError(
            expected='metadata 的值可以 deepcopy（spawn 会再拷一次）',
            received=f'{type(exc).__name__}: {exc}',
            next_action='换成可拷贝的值；不要把锁、套接字或活对象放进 metadata',
        ) from exc
    return MappingProxyType(copied)


def _slots(value: object) -> tuple[AgentSlot, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ConfigError(
            expected='slots 是 AgentSlot 的列表或元组',
            received=(
                f'slots={value!r}，类型是 {type(value).__name__}'
                + (
                    '：str / bytes 也是 Sequence，按序列收下会把字符串拆成单个字符'
                    if isinstance(value, (str, bytes, bytearray))
                    else ''
                )
            ),
            next_action='写成 (AgentSlot(...), ...)，或省略（空拓扑合法）',
        )
    items = cast(Sequence[object], value)
    bad = [index for index, item in enumerate(items) if not isinstance(item, AgentSlot)]
    if bad:
        kinds = [type(items[index]).__name__ for index in bad]
        raise ConfigError(
            expected='slots 的每一项都是 AgentSlot',
            received=f'下标 {bad!r} 的类型是 {kinds!r}',
            next_action='用 AgentSlot(...) 构造每一项，不要传 dict 或别的对象',
        )
    return tuple(cast(AgentSlot, item) for item in items)


def _reject_duplicate_names(slots: tuple[AgentSlot, ...]) -> None:
    counts: dict[str, int] = {}
    order: list[str] = []
    for slot in slots:
        if slot.name not in counts:
            order.append(slot.name)
        counts[slot.name] = counts.get(slot.name, 0) + 1
    duplicated = [name for name in order if counts[name] > 1]
    if not duplicated:
        return
    detail = [f'{name}×{counts[name]}' for name in duplicated]
    raise ConfigError(
        expected='拓扑里每个槽名唯一',
        received=f'重复的槽名 {detail!r}',
        next_action='改掉重名。槽名是寻址键，两个同名槽会让 resolve 不确定指向哪一个',
    )


def _reject_extra_roots(slots: tuple[AgentSlot, ...]) -> None:
    roots = [slot.name for slot in slots if slot.is_root]
    if len(roots) <= 1:
        return
    raise ConfigError(
        expected='拓扑最多一个 is_root=True 的槽；零个 root 合法',
        received=f'root 槽 {roots!r}',
        next_action='只留一个 root，或全部改成 is_root=False（纯工人拓扑）',
    )
