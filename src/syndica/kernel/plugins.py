"""Runtime 级插件——扩展装配的第二层（spec §5.3）。

**跨 Agent 通用、且永不暴露为开关**的逻辑放这里。带开关的能力一律建模为 Runtime Module（§5.5）
——这条判据决定了本模块刻意很薄：一个插件只有名字和一次注册机会，没有配置、没有启停钩子、没有
服务注入。那些都是 Module 的事。

**插件列表的次序是配置的一部分。** 可拦截切点按注册先后串行，因此换个次序就换了行为。

**一次被拒的装配不在 hook 中心上留下任何 hook 或分类声明。** 装到一半失败的话，宿主手里就是一个
构造失败、却已经改过 hook 中心的 Runtime，而 `HooksCenter` 没有任何拆除入口。两道防线：校验全部做完
才开始注册（内核自己判得出的那些错——重名、不是实例、缺 name/register），以及注册期按快照回滚（插件
自己的 `register` 抛出，那是第三方代码，校验判不出来）。

保证只覆盖**这个中心**：`register` 是第三方代码，它在中心之外做的事（起任务、写文件、往别的中心
注册）回滚不到，那些得由插件自己保证。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks import HooksCenter

__all__ = ['Plugin', 'install_plugins']


class Plugin(Protocol):
    """一个 Runtime 级插件。

    `name` 用来查重与排障：症状出现在 hook 的效果上，而排查的第一个问题永远是「装了哪些、按什么
    顺序」。
    """

    @property
    def name(self) -> str: ...

    def register(self, hooks: HooksCenter) -> None: ...


def _not_a_sequence(plugins: object) -> ConfigError:
    return ConfigError(
        expected='plugins 是一个插件序列（list / tuple），次序即执行次序',
        received=f'收到 {type(plugins).__name__} 类型的 {plugins!r}',
        next_action=(
            '把它包成 list：plugins=[the_plugin]。'
            '单个插件直接传进来会被逐元素迭代，报出的错误指着 plugins.py 内部而不是你写下它的那行'
        ),
    )


def _is_a_class(plugin: object, index: int) -> ConfigError:
    return ConfigError(
        expected=f'plugins[{index}] 是插件**实例**',
        received=f'收到类对象 {cast(type, plugin).__name__} 本身',
        next_action=(
            f'加上括号：{cast(type, plugin).__name__}(...)。'
            '类对象上的 name 与 register 都取得到，因此逐项检查放不倒它，'
            '而 register(hooks) 会把 hooks 绑到 self 上，报出的 TypeError 指着插件自己的签名'
        ),
    )


def _has_no_name(plugin: object, index: int, name: object) -> ConfigError:
    return ConfigError(
        expected='每个插件有一个非空的 name（用于查重与排障）',
        received=f'plugins[{index}] 是 {type(plugin).__name__}，它的 name 是 {name!r}',
        next_action=(
            '给这个插件加一个返回非空字符串的 name 属性；'
            '插件的症状出现在 hook 的效果上，没有名字就答不出「装了哪些、按什么顺序」'
        ),
    )


def _has_no_register(plugin: object, index: int, name: str) -> ConfigError:
    return ConfigError(
        expected='每个插件有一个 register(hooks) 方法，在里面挂自己的 hook',
        received=(
            f'plugins[{index}]（name={name!r}）是 {type(plugin).__name__}，它没有可调用的 register'
        ),
        next_action=(
            '给它加 def register(self, hooks: HooksCenter) -> None，'
            '在里面调 hooks.register(junction, cb)'
        ),
    )


def _duplicate_name(index: int, name: str) -> ConfigError:
    return ConfigError(
        expected='每个插件只装一次：插件名在一个 Runtime 内唯一',
        received=f'plugins[{index}] 的 name={name!r} 与前面某一项重复',
        next_action=(
            '去掉重复的那一项。同一个能力装两遍，它的 hook 会跑两遍——'
            '计数翻倍、改写叠加，而症状不会指向「装了两次」'
        ),
    )


def _validated_sequence(plugins: object) -> Sequence[object]:
    """校验插件清单是一个序列。

    参数标注为 object 而非 Sequence[Plugin]：要挡的就是违反标注的运行时取值。收窄了下面的
    isinstance 会变成 pyright 眼里的死代码，而这道检查存在的理由正是静态检查不在场的那些调用。
    `str` / `bytes` 单独挡：它们也是 Sequence，逐字符迭代之后报出的是一串「name 不是非空 str」，
    指着每一个字符而不是指着那次误传。
    """
    if isinstance(plugins, str | bytes) or not isinstance(plugins, Sequence):
        raise _not_a_sequence(plugins)
    return cast('Sequence[object]', plugins)


def _validated(plugin: object, index: int) -> tuple[Plugin, str]:
    """逐项检查，而不是 `isinstance(plugin, Plugin)`：错在哪一项、缺的是哪一样都要说出来。

    `runtime_checkable` 的 Protocol 对**类对象**无感——类上 `name` 与 `register` 都取得到，于是
    「忘了加括号」会通过检查，再在 `register(hooks)` 把 hooks 绑到 self 上时炸在插件自己的签名上。
    因此类对象单独挡，且挡在取 name 之前。

    **交回校验过的那个 name**，`name` 因此全程只读一次：它可以是每次交回不同值的 property，再读一遍
    就是再取一个未经校验的值——查重比对的、最终交给宿主排障的会与校验过的那个不是同一个。
    """
    if isinstance(plugin, type):
        raise _is_a_class(plugin, index)
    name = getattr(plugin, 'name', None)
    if not isinstance(name, str) or not name.strip():
        raise _has_no_name(plugin, index, name)
    if not callable(getattr(plugin, 'register', None)):
        raise _has_no_register(plugin, index, name)
    # 上面三道检查就是这次转换的全部依据：Plugin 是 Protocol，运行时没有更强的检查可做。
    return cast(Plugin, plugin), name


def install_plugins(hooks: HooksCenter, plugins: Sequence[Plugin]) -> tuple[str, ...]:
    """按给定顺序装配插件，返回它们的名字（快照）。

    参数标注为 `Sequence[Plugin]` 而运行期仍检查：要挡的就是违反标注的运行时取值（配置里读来的
    清单、忘了包 list 的单个插件）。

    任一插件的 `register` 抛出时，中心还原到进来时的样子再上抛（见模块 docstring 的两道防线）。
    """
    checked: list[Plugin] = []
    installed: list[str] = []
    for index, plugin in enumerate(_validated_sequence(plugins)):
        candidate, name = _validated(plugin, index)
        if name in installed:
            raise _duplicate_name(index, name)
        checked.append(candidate)
        installed.append(name)
    snapshot = hooks._assembly_snapshot()  # pyright: ignore[reportPrivateUsage]
    try:
        for plugin in checked:
            plugin.register(hooks)
    except BaseException:
        hooks._assembly_restore(snapshot)  # pyright: ignore[reportPrivateUsage]
        raise
    return tuple(installed)
