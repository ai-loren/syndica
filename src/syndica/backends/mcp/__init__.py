"""外部 MCP server 的客户端。只依赖 kernel。"""

from syndica.backends.mcp.process import ExternalProcess, ProcessSupervisor

__all__ = ['ExternalProcess', 'ProcessSupervisor']
