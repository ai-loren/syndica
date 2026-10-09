"""doctor 的 ACP 自检：按 run 的方式拉起托管的 ACP Agent，只做 `initialize`，然后整组收掉。

环境与 `AcpAgentModel.bind_runtime` 同一条规则：Agent 环境整份，加上 `pass_env` 点名、宿主环境里
确实有的变量，不是在宿主环境上追加。声明的能力也与 run 相同，否则对端协商出的结果不是 run 会拿到的。
上限只约束握手；关闭走 `ExternalProcess` 的终止阶梯，成功、失败、超时、被取消都走，孙进程一起收。
对端自报的名字与版本是不可信的文字：截断后交回，转义与脱敏由调用方做。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from acp.client.connection import ClientSideConnection
from acp.schema import ClientCapabilities, FileSystemCapabilities

from syndica.backends.acp.process import ExternalProcess
from syndica.kernel import ConfigError, Redactor

__all__ = ['AcpHello', 'probe_acp']

_REPORTED_CAP = 80


@dataclass(frozen=True)
class AcpHello:
    """握手的结果。`agent_name` / `agent_version` 是对端自报的；没报就是 None。"""

    protocol_version: int
    agent_name: str | None
    agent_version: str | None


class _Handshake:
    """握手期间没有会话：对端的通知收下不管，回调请求由 SDK 按缺省答。"""

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del session_id, update, kwargs


async def probe_acp(
    name: str,
    command: Sequence[str],
    *,
    pass_env: Sequence[str],
    agent_env: Mapping[str, str],
    host_env: Mapping[str, str],
    cwd: str,
    redactor: Redactor,
    timeout: float,  # noqa: ASYNC109
) -> AcpHello:
    """拉起、握手、收掉，再交回协商结果。起不来、超时、断线、版本不是 1，一律三件套。"""
    process = ExternalProcess(
        command, env=_hosted_env(agent_env, pass_env, host_env), cwd=cwd, redactor=redactor
    )
    try:
        async with process:
            writer, reader = process.streams()
            conn = ClientSideConnection(_Handshake(), writer, reader)  # pyright: ignore[reportArgumentType]
            try:
                async with asyncio.timeout(timeout):
                    initialized = await conn.initialize(
                        protocol_version=1,
                        client_capabilities=ClientCapabilities(
                            fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                            terminal=True,
                        ),
                    )
            finally:
                with contextlib.suppress(Exception):
                    await conn.close()
    except TimeoutError:
        raise ConfigError(
            expected=f'acp:{name} 在 {timeout:g} 秒内答复 initialize',
            received=f'{timeout:g} 秒内没有答复；进程已整组收掉',
            next_action=(
                '在终端里单独跑这条 command，确认它启动后在 stdout 上说 ACP；要先下载的命令'
                '（npx 之类）先在终端里跑一次让它装好'
            ),
        ) from None
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            expected=f'acp:{name} 拉起之后答完 initialize',
            received=f'答复之前连接断了（{type(exc).__name__}）；进程已整组收掉',
            next_action='在终端里单独跑这条 command 看它为什么退出；它的 stderr 经脱敏记在日志里',
        ) from None
    version = getattr(initialized, 'protocol_version', None)
    if type(version) is not int or version != 1:
        raise ConfigError(
            expected=f'acp:{name} 协商到 ACP 协议版本 1',
            received=f'对端给出 protocolVersion={version!r}',
            next_action=f'换一个说协议版本 1 的 ACP Agent，或把 acp:{name} 换到说版本 1 的发行版',
        )
    info = getattr(initialized, 'agent_info', None)
    return AcpHello(
        protocol_version=version,
        agent_name=_reported(getattr(info, 'name', None)),
        agent_version=_reported(getattr(info, 'version', None)),
    )


def _hosted_env(
    agent_env: Mapping[str, str], names: Sequence[str], host_env: Mapping[str, str]
) -> dict[str, str]:
    env = dict(agent_env)
    for key in names:
        value = host_env.get(key)
        if value is not None:
            env[key] = value
    return env


def _reported(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:_REPORTED_CAP]
