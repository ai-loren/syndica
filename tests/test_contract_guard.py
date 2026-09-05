"""守卫只在配置坏掉时才发声，配置正确的套件跑不到它的断言——一次重构把它改哑，默认套件依旧
全绿，而第三方的 async 用例又开始静默跳过。这里用子进程把「插件缺失」那种环境造出来。

本文件刻意不放在 tests/contracts/ 下：子进程跑的正是那个目录，同处一处会无限递归。
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = 'test_async_cases_are_driven_by_an_async_plugin'


def test_guard_fails_every_contract_suite_when_no_async_plugin_drives_it():
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'pytest',
            'tests/contracts/',
            '-p',
            'no:asyncio',
            '-p',
            'no:cacheprovider',
            '-q',
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    guard_failures = sorted(
        line.split('::')[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith('FAILED') and GUARD in line
    )
    suites = sorted(
        line.split('::')[1]
        for line in proc.stdout.splitlines()
        if line.startswith('FAILED') and GUARD in line
    )
    assert len(guard_failures) == 2, (
        f'期望没有 async 插件时两套契约的 {GUARD} 都失败。'
        f' 实际失败的是 {suites}。'
        ' 守卫被改哑了，或者某套契约没继承 ContractSuite——两种情况下第三方都会看到 async'
        ' 用例被静默跳过却以为自己合规。'
    )
