"""守卫只在配置坏掉时才发声，配置正确的套件跑不到它的断言——一次重构把它改哑，默认套件依旧
全绿，而第三方的 async 用例又开始静默跳过。这里用子进程把它该发声、和它该闭嘴的两种环境都造
出来：插件缺失必须失败，选择过滤把 async 用例全滤掉必须放行。

本文件刻意不放在 tests/contracts/ 下：子进程跑的正是那个目录，同处一处会无限递归。
"""

import subprocess
import sys
from pathlib import Path

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


def _collected_guards() -> set[str]:
    """现有契约套件各自那份守卫的 nodeid。数量由实际收集到的套件决定，新增第三套无需改本文件。"""
    proc = _run_contracts('--collect-only', '-k', GUARD)
    return {line.strip() for line in proc.stdout.splitlines() if GUARD in line}


def _failed_guards(stdout: str) -> set[str]:
    return {
        line.removeprefix('FAILED ').split(' - ')[0].strip()
        for line in stdout.splitlines()
        if line.startswith('FAILED') and GUARD in line
    }


def test_guard_fails_every_contract_suite_when_no_async_plugin_drives_it():
    expected = _collected_guards()
    assert expected, (
        f'期望 tests/contracts/ 下能收集到 {GUARD}，它是本文件的比对基准。'
        ' 实际一个都没收集到。'
        f' 跑 `uv run pytest tests/contracts/ --collect-only -k {GUARD}` 看是收集坏了，'
        ' 还是所有套件都不再继承 ContractSuite。'
    )
    failed = _failed_guards(_run_contracts('-p', 'no:asyncio').stdout)
    assert failed == expected, (
        f'期望没有 async 插件时每套契约的 {GUARD} 都失败：{sorted(expected)}。'
        f' 实际失败的是 {sorted(failed)}。'
        ' 差集里的套件要么没继承 ContractSuite、要么守卫被改哑了；'
        ' 去 contracts/base.py 恢复断言或让该套件继承 ContractSuite，'
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
