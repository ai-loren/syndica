# HTTP 契约变更记录

[`openapi.json`](openapi.json) 是 `syndica up` 交出的 HTTP / SSE / WebSocket 面的快照，由
`syndica.server.models` 生成（`SYNDICA_UPDATE_OPENAPI=1 uv run pytest tests/server/test_openapi.py`）。
快照每变一次，这里记一条：日期、快照 sha256 的前 12 位、改了什么、是不是破坏性变更。用例按摘要判，
漏记一条会变红。

标 `x-syndica-stability: provisional` 的字段来自内核里 M12 才冻结的类型（预算视图、进度），在那之前
形状仍可能变，变了照样记在这里。

## 2026-10-06 · `99c4b2720a3e` · 首版

- 路由：`health`、`login`、`doctor`、`roles`、`sessions`（列表、新建、恢复、关闭、删除）、
  `sessions/{sid}/snapshot`、会话与全局 SSE。
- SSE：事件 id 是 `<epoch>:<seq>`，`reset` 帧没有 id；每种事件的 payload 必有键在 `x-syndica-events`。
- WebSocket RPC：`auth`（第一帧）、`send`、`answer`、`abort`、`grant`、`budget_answer`，帧格式在
  `x-syndica-rpc`。

## 2026-10-07 · `bc7abfde2e3a` · 产物、Rubric、trace、终端（追加，不破坏）

- 路由：`topologies`、`traces`（列表与分析）、`sessions/{sid}/trace`、`sessions/{sid}/flags`、
  `sessions/{sid}/artifacts`（列表、版本、原文、预览、diff、反馈）、`sessions/{sid}/rubrics`。
- 原文下载一律是附件、`application/octet-stream`、`Content-Security-Policy: sandbox`，登记的 mime 在
  `X-Syndica-Mime` 里。
- WebSocket：人的终端 `sessions/{sid}/pty`、Agent 终端的只读镜像 `sessions/{sid}/agent-pty/{handle}`
  （`x-syndica-pty`）；RPC 加 `activate_rubric`。
- 事件：内核加 `progress_measured`；快照的 `progress` 是每个 Agent 最近一次量到的进度。
