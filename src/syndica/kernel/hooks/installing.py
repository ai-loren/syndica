"""装配期身份（M4 D5）：此刻在跑的是哪一段第三方装配代码。

只在内核枚举的几处设：插件的 `register`（Runtime 插件 = 插件名，Module 插件 = Module 名）、Module 的
服务工厂、`on_start`、工具工厂与插件工厂（= Module 名）。只用于声明扩展切点时核对 owner 段；运行期的
触发靠持有句柄，不靠它——环境身份会经子任务、服务调用与并行分发泄漏。`_drive`、收割任务与关机任务
开头清掉它。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

__all__ = ['Installer', 'current_installer', 'installing', 'no_installer']


@dataclass(frozen=True)
class Installer:
    kind: Literal['plugin', 'module']
    name: str

    def describe(self) -> str:
        return f'Runtime 插件 {self.name!r}' if self.kind == 'plugin' else f'Module {self.name!r}'


_INSTALLER: ContextVar[Installer | None] = ContextVar('syndica_installer', default=None)


@contextmanager
def installing(kind: Literal['plugin', 'module'], name: str) -> Generator[None, None, None]:
    token = _INSTALLER.set(Installer(kind, name))
    try:
        yield
    finally:
        _INSTALLER.reset(token)


def current_installer() -> Installer | None:
    return _INSTALLER.get()


def no_installer() -> None:
    """任务开头调：任务建在谁的上下文里就继承了谁的装配期身份，而任务里跑的已经不是那段装配。"""
    _INSTALLER.set(None)
