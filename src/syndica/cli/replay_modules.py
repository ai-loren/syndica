"""回放重建第三方 Module 的判据（M4 D13）：从不让**文档**决定要不要跑第三方代码。

检查次序是承重的：当前工作区的配置受信任 → 它启用了这个 Module → 装着的发行版名与版本等于记录里的，
三项都过了才 import 入口点、读装着的那一份的 `replay_safe`。trace 目录 Agent 写得进去，先 import
就让一份伪造的 trace 能叫 `trace replay` import 任何装着的入口点。信任是被动查的：`trace` 命令不经
信任确认，回放从不发问。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from syndica.cli.modules import EntryPointSource, discover, load_module
from syndica.cli.trust import (
    JsonTrustStore,
    PinnedImports,
    config_of,
    private_pycache_prefix,
    read_snapshot,
    store_path,
    within,
)
from syndica.kernel import ConfigError, ModuleHost, RuntimeModule
from syndica.kernel.scenario_schema import ModuleRecord

__all__ = ['module_rebuilder']

_TRUST = '先在这个工作区运行 syndica config trust（或 run / doctor 时确认），再回放'


def module_rebuilder(
    *, workspace_root: Path, environ: Mapping[str, str], entry_points: EntryPointSource
) -> Callable[[ModuleRecord, ModuleHost], RuntimeModule | str]:
    """回放用的判据：交回重建好的 Module，或一句为什么不重建（带上下一步）。"""

    def rebuild(record: ModuleRecord, host: ModuleHost) -> RuntimeModule | str:
        config_file = workspace_root / '.syndica.yaml'
        trusted = store_path(environ)
        if trusted is None or not config_file.is_file():
            return f'这个工作区没有受信任的 .syndica.yaml：{_TRUST}'
        if within(trusted, workspace_root):
            return (
                f'信任存储 {trusted} 在工作区之内，Agent 改得到它：'
                '把 XDG_CONFIG_HOME 或 SYNDICA_TRUST_STORE 指到工作区之外再回放'
            )
        discovered = discover(entry_points)
        try:
            snapshot = read_snapshot(config_file, workspace_root, discovered)
        except ConfigError as error:
            return f'读工作区配置失败（{error.received}）：修好配置之后{_TRUST}'
        if JsonTrustStore(trusted).get(snapshot.path) != snapshot.digest:
            return f'这份工作区配置还没被信任，或信任之后改过：{_TRUST}'
        if record.name not in config_of(snapshot).modules:
            return '这份工作区配置没有启用它：在 modules 里写上它、确认配置之后再回放'
        entry = next((item for item in discovered if item.name == record.name), None)
        if entry is None or entry.problem is not None:
            reason = '没有装' if entry is None else entry.problem
            return f'它在这台机器上装不上（{reason}）：装上记录里的那个发行版再回放'
        installed = (entry.distribution, entry.version)
        if installed != (record.distribution, record.distribution_version):
            return (
                f'装着的是 {entry.distribution} {entry.version}，记录里是 '
                f'{record.distribution} {record.distribution_version}：装上记录里的版本再回放'
            )
        inside = [source for source in snapshot.module_sources if source.inside]
        if inside:
            # 与 run 一样：工作区里种下的 `.pyc` 不当缓存用，import 时按确认时的内容核对。回放这个
            # 进程里一直生效。
            sys.pycache_prefix = private_pycache_prefix()
            PinnedImports(inside).install()
        try:
            module = load_module(entry, host, host.redactor)
        except ConfigError as error:
            return f'import 它失败（{error.received}）：修好这个发行版的安装之后再回放'
        if not module.replay_safe:
            return (
                '装着的那一份没声明 replay_safe：它的代码不能在回放里再跑一遍'
                '（要回放，请作者确认它不做 I/O 之后声明 replay_safe）'
            )
        if module.version != record.module_version:
            return (
                f'装着的 Module 版本是 {module.version}，记录里是 {record.module_version}：'
                '装上记录时的那个版本再回放'
            )
        return module

    return rebuild
