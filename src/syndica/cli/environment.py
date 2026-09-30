"""Agent 的命令环境（spec D6）：宿主读自己的环境，只把白名单交给 Agent 的工作区。

内核不读环境变量，所以这件事在 surface 做。run、chat、doctor 三处都经 `agent_environment`，Agent 在
哪条路径上都看到同一份环境。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from syndica.kernel import ConfigError

__all__ = [
    'FIXED_PASS_ENV',
    'HOST_SET_ENV',
    'AgentEnvironment',
    'agent_environment',
    'looks_secret',
    'reopens_git',
    'valid_env_name',
]

# 总是透传的变量（都只取宿主里实际存在的）：让 uv、python、git 在 Agent 的 Bash 里能跑起来。
FIXED_PASS_ENV: tuple[str, ...] = (
    'PATH',
    'HOME',
    'USER',
    'LOGNAME',
    'LANG',
    'LC_ALL',
    'LC_CTYPE',
    'TMPDIR',
    'XDG_CACHE_HOME',
    'UV_CACHE_DIR',
)
# 名字按 `_` 切开后，任一词（不分大小写）落在这里就不交给 Agent。按词而不是按子串：
# `GIT_AUTHOR_NAME` 不是密钥，`SSH_AUTH_SOCK` 是（Agent 不拿 ssh agent）。
_SECRET_WORDS = frozenset(
    {
        'AUTH',
        'COOKIE',
        'CREDENTIAL',
        'CREDENTIALS',
        'KEY',
        'KEYS',
        'PASS',
        'PASSWD',
        'PASSWORD',
        'PAT',
        'SECRET',
        'SECRETS',
        'TOKEN',
        'TOKENS',
    }
)
# 宿主自己设的值；`pass_env` 不能透传它们，否则 Agent 就拿回了用户的 git 与 gh 配置。
HOST_SET_ENV: tuple[str, ...] = (
    'GIT_TERMINAL_PROMPT',
    'GIT_CONFIG_NOSYSTEM',
    'GIT_CONFIG_GLOBAL',
    'GH_CONFIG_DIR',
    'GH_TOKEN',
    'GH_ENTERPRISE_TOKEN',
)
# 这些名字会让 Agent 的 git 重新用上配置、凭证、别的仓库或别的可执行文件，`pass_env` 同样不收。
_GIT_REOPENERS = re.compile(
    r'\A(GIT_CONFIG.*|GIT_SSH.*|.*ASKPASS|GIT_DIR|GIT_WORK_TREE|GIT_EXEC_PATH|GIT_NAMESPACE|GH_.*)\Z'
)
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
# Agent 的全局 git 配置只有这一项：工作区里种下的裸仓库不带 `--bare` 也会被 git 用上（默认 `all`），
# 它的 config 与别名于是生效。这一项只在全局这类受保护配置里才被 git 认。
_GIT_CONFIG = '[safe]\n\tbareRepository = explicit\n'
# gh 在配置目录里找不到登录时会去系统钥匙串里取令牌，空的 GH_CONFIG_DIR 挡不住。设一个占位令牌，
# gh 就用它、认证失败，Agent 失手调 gh 不会在远端留下任何效果。刻意去钥匙串里取（`gh auth token
# --user`、`security find-generic-password`）挡不住，见 D5。
_NO_GH_LOGIN = 'syndica-agent-has-no-github-login'


def looks_secret(name: str) -> bool:
    """这个变量名像不像装着密钥：按 `_` 切词，任一词是 KEY / TOKEN / AUTH 这类。"""
    return any(word.upper() in _SECRET_WORDS for word in name.split('_'))


def valid_env_name(name: str) -> bool:
    return bool(_NAME.match(name))


def reopens_git(name: str) -> bool:
    """这个变量会不会让 Agent 的 git / gh 重新用上宿主之外的配置、凭证或仓库。"""
    return bool(_GIT_REOPENERS.match(name))


@dataclass(frozen=True)
class AgentEnvironment:
    """一次 run 的 Agent 环境：交给工作区的变量表，以及 run 结束时要删掉的临时目录。"""

    values: Mapping[str, str]
    scratch: Path

    def remove(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)


def agent_environment(
    host: Mapping[str, str], pass_env: Sequence[str], *, workspace_root: Path
) -> AgentEnvironment:
    """按固定白名单与 `pass_env` 从宿主环境里取值，再加上宿主自己设的四个值。

    `pass_env` 已在读配置时校验过（不像密钥、不是宿主要设的名字）。临时目录建在工作区外：建在里面，
    Agent 就能改写自己的 git 全局配置（比如设一个 `core.fsmonitor` 命令）。
    """
    values = {
        name: host[name] for name in dict.fromkeys((*FIXED_PASS_ENV, *pass_env)) if name in host
    }
    scratch = Path(tempfile.mkdtemp(prefix='syndica-agent-')).resolve()
    try:
        if _inside(scratch, workspace_root):
            raise ConfigError(
                expected='Agent 的临时 git / gh 配置建在工作区之外',
                received=(
                    f'系统临时目录 {str(scratch.parent)!r} 在工作区 {str(workspace_root)!r} 之内'
                ),
                next_action=(
                    '把 TMPDIR 指到工作区外的目录再运行；放在工作区里，Agent 就能改写它自己的 '
                    'git 配置'
                ),
            )
        git_config = scratch / 'gitconfig'
        git_config.write_text(_GIT_CONFIG, encoding='utf-8')
        gh_config = scratch / 'gh'
        gh_config.mkdir()
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    values.update(
        {
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': str(git_config),
            'GH_CONFIG_DIR': str(gh_config),
            'GH_TOKEN': _NO_GH_LOGIN,
            'GH_ENTERPRISE_TOKEN': _NO_GH_LOGIN,
        }
    )
    return AgentEnvironment(values=MappingProxyType(values), scratch=scratch)


def _inside(path: Path, root: Path) -> bool:
    """path 是不是 root 或在它之下。按目录身份（设备号加 inode）比，不按拼写：APFS 默认不分大小写，
    `/tmp/WS/.tmp` 与 `/tmp/ws/.tmp` 是同一个目录，而拼写比较会把它当成工作区外。"""
    try:
        root_identity = root.stat()
    except OSError:
        # 根不存在就谈不上「在它里面」；缺根由 LocalWorkspace 报那条说得清的错。
        return False
    return any(os.path.samestat(root_identity, parent.stat()) for parent in (path, *path.parents))
