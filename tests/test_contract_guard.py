"""守卫只在配置坏掉时才发声，配置正确的套件跑不到它的断言——一次重构把它改哑，默认套件依旧
全绿，而第三方的 async 用例又开始静默跳过。这里用子进程把它该发声、和它该闭嘴的两种环境都造
出来：插件缺失必须失败，选择过滤把 async 用例全滤掉必须放行。

「该有几个守卫」只能数源码包里的契约套件，不能数 pytest 收集到的守卫：某套契约不再继承
ContractSuite、或没有任何测试类继承它时，它会同时从「期望」和「实际」两侧消失，同源的比对
因此永真——本该被抓住的静默失效反而通过。

本文件刻意不放在 tests/contracts/ 下：子进程跑的正是那个目录，同处一处会无限递归。
"""

import subprocess
import sys
from importlib import import_module
from inspect import getmembers, isclass
from pathlib import Path
from pkgutil import iter_modules

from syndica.kernel import contracts
from syndica.kernel.contracts import ContractSuite

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = 'test_async_cases_are_driven_by_an_async_plugin'


def _run_contracts(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            '-m',
            'pytest',
            'tests/contracts/',
            '-p',
            'no:cacheprovider',
            '-q',
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _contract_suites() -> dict[str, frozenset[str]]:
    """包里定义的每套契约 → 它自己声明的用例名。

    用例名当指纹，用来在收集结果里认出是谁在跑这套契约。GUARD 不进指纹：它是所有套件共有的，
    拿它定位会同时命中别套契约的测试类。
    """
    suites: dict[str, frozenset[str]] = {}
    for module_info in iter_modules(contracts.__path__):
        module = import_module(f'{contracts.__name__}.{module_info.name}')
        for name, obj in getmembers(module, isclass):
            if obj.__module__ != module.__name__ or obj is ContractSuite:
                continue
            cases = frozenset(
                case for case in vars(obj) if case.startswith('test_') and case != GUARD
            )
            if cases:
                suites[name] = cases
    return suites


def _collected_nodeids() -> set[str]:
    proc = _run_contracts('--collect-only')
    return {
        line.strip()
        for line in proc.stdout.splitlines()
        if line.startswith('tests/') and '::' in line
    }


def _test_classes_running(collected: set[str], cases: frozenset[str]) -> set[str]:
    """收集结果里跑了 cases 中任一用例的测试类前缀（`路径::类名`）。"""
    classes: set[str] = set()
    for nodeid in collected:
        prefix, _, case = nodeid.rpartition('::')
        if case in cases:
            classes.add(prefix)
    return classes


def _failed_guards(stdout: str) -> set[str]:
    return {
        line.removeprefix('FAILED ').split(' - ')[0].strip()
        for line in stdout.splitlines()
        if line.startswith('FAILED') and GUARD in line
    }


def test_every_contract_suite_in_the_package_is_guarded_when_no_async_plugin_drives_it():
    suites = _contract_suites()
    assert suites, (
        f'期望在 {contracts.__name__} 里枚举到至少一套契约，它是本文件的比对基准。'
        ' 实际一套都没枚举到，于是下面的逐套检查一条都不会执行。'
        f' 去看 {contracts.__name__} 下的套件模块还在不在、套件类是否还声明 test_ 方法；'
        ' 枚举为空必须判失败，否则这个检查会退化成空转通过。'
    )
    collected = _collected_nodeids()
    failed = _failed_guards(_run_contracts('-p', 'no:asyncio').stdout)
    for suite, cases in sorted(suites.items()):
        classes = _test_classes_running(collected, cases)
        assert classes, (
            f'期望 tests/contracts/ 下有测试类在跑 {suite}，它随发行版发布，必须有实现真的验它。'
            f' 实际按 {suite} 自己声明的 {len(cases)} 个用例名比对，一个测试类都没匹配上。'
            f' 让某个测试类继承 {suite} 并提供它要的 fixture，'
            ' 否则这套契约整体静默失效，用例总数掉下来也没人出声。'
        )
        guards = {f'{cls}::{GUARD}' for cls in classes}
        assert guards & failed, (
            f'期望没有 async 插件时 {suite} 的守卫失败，候选是 {sorted(guards)}。'
            f' 实际失败的守卫是 {sorted(failed)}，候选一个都不在其中。'
            f' 让 {suite} 继承 ContractSuite（守卫由它提供），'
            ' 或去 contracts/base.py 恢复被改哑的断言；'
            ' 否则第三方会看到 async 用例被静默跳过却以为自己合规。'
        )


def test_guard_declines_when_a_selection_filter_leaves_no_async_case_collected():
    proc = _run_contracts('-k', GUARD)
    assert proc.returncode == 0, (
        f'期望 -k 把 async 用例全滤掉时 {GUARD} 不下结论而是通过，exit 0。'
        f' 实际 exit {proc.returncode}。'
        ' 标记是 collection 期打上的，没被收集的 async 用例根本没有标记可读，'
        ' 把「读不到标记」当成插件缺失会让 -k / --lf / xdist 分片下的正常运行变红；'
        f' 去 contracts/base.py 让守卫在收集数为零时早退，或跑'
        f' `uv run pytest tests/contracts/ -k {GUARD}` 看它到底为什么红。'
    )
