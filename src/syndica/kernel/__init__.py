"""Kernel: runtime, agent, tools, hooks, protocols.

零重依赖：只允许标准库 + pydantic + 一个 HTTP 客户端。
不读环境变量：路径 / 凭证 / 开关由宿主注入。
"""
