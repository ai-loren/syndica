"""内核内置工具（spec §5.6）。"""

from syndica.kernel.tools.human import AskUser, AskUserQuestion, HumanInteractionModule
from syndica.kernel.tools.orchestration import (
    Delegate,
    Fork,
    KillAgent,
    QueryAgent,
    SendMessage,
    orchestration_tools,
)

__all__ = [
    'AskUser',
    'AskUserQuestion',
    'Delegate',
    'Fork',
    'HumanInteractionModule',
    'KillAgent',
    'QueryAgent',
    'SendMessage',
    'orchestration_tools',
]
