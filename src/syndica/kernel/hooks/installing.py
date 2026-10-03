"""装配期身份（M4 D5）：此刻在跑的是哪一段第三方装配代码。

只在内核枚举的几处设：插件的 `register`（插件 = 插件名，Module 插件 = Module 名）、Module 的服务
工厂、`on_start`、工具工厂与插件工厂（= Module 名）。只用于声明扩展切点时核对 owner 段；运行期的触发
靠持有句柄，不靠它。

它随 contextvar 进入装配代码里建的任务（比如服务工厂里 `create_task`），在那些任务里声明切点仍按
装配期的规则判：只会更严，不会更松。内核自己建的任务（`_drive`、收割、关机）开头清掉它。

装配期之后它还在两处设：分发时每个 hook 回调按**注册它的那段代码**的身份跑，Module 装的工具的
`execute` 按那个 Module 的身份跑。hook 的作者按注册时的身份记，而回调里、工具里再注册的 hook 不设
就记成宿主的——被收紧的 Module 于是能在装配期之后挂一个改写，绕过按作者的收紧。`on_shutdown` 没有
身份。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

__all__ = ['Installer', 'acting_as', 'current_installer', 'installing', 'no_installer']


@dataclass(frozen=True)
class Installer:
    kind: Literal['plugin', 'module']
    name: str

    def describe(self) -> str:
        return f'插件 {self.name!r}' if self.kind == 'plugin' else f'Module {self.name!r}'


_INSTALLER: ContextVar[Installer | None] = ContextVar('syndica_installer', default=None)


@contextmanager
def installing(kind: Literal['plugin', 'module'], name: str) -> Generator[None, None, None]:
    token = _INSTALLER.set(Installer(kind, name))
    try:
        yield
    finally:
        _INSTALLER.reset(token)


@contextmanager
def acting_as(installer: Installer | None) -> Generator[None, None, None]:
    """按 `installer` 的身份跑一段代码；None 是宿主（装配期之外的代码）。"""
    token = _INSTALLER.set(installer)
    try:
        yield
    finally:
        _INSTALLER.reset(token)


def current_installer() -> Installer | None:
    return _INSTALLER.get()


def no_installer() -> None:
    """任务开头调：任务建在谁的上下文里就继承了谁的装配期身份，而任务里跑的已经不是那段装配。"""
    _INSTALLER.set(None)
