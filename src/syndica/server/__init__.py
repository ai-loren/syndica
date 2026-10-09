"""Web / HTTP 产品面（spec §9）。只依赖 kernel；会话怎么装由组合根注入（`protocols`）。"""

from syndica.server.app import MAX_STREAMS, ProtocolMount, ServerSettings, create_app
from syndica.server.auth import Credentials
from syndica.server.live import Live
from syndica.server.protocols import AskBudget, SessionAssembly, SessionHandle

__all__ = [
    'MAX_STREAMS',
    'AskBudget',
    'Credentials',
    'Live',
    'ProtocolMount',
    'ServerSettings',
    'SessionAssembly',
    'SessionHandle',
    'create_app',
]
