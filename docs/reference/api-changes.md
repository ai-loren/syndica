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

## 2026-10-08 · `2bffe79d1f69` · WebSocket 的拒绝、交付说明、描述不带版本号（追加，不破坏）

- WebSocket 被拒时先收一帧 `RpcReply`（`ok: false`，`error` 是三件套），再按关闭码关闭：4401 没认证、
  4404 会话没打开、4408 等认证的连接太多被挤掉（新）、1013 已认证的流与 WebSocket 满了
  （`x-syndica-rpc.refusal`）。没认证的连接不再占 16 个名额。
- `reset` 只有一种形状：没有 `id:`，`data` 是 `{}`。内核总线丢过事件时发的 reset 也是这一种（此前那一种
  带 id、带 `reason` / `dropped`，与这里写的契约不符）。
- 预算请求体（建会话、`grant`、`budget_answer`）按严格类型校验：`true`、`"3"`、`1.0` 当整数都返回 422。
- 事件 `artifact_registered` 加必有键 `note`（`Deliver` 时模型写的说明，没有是空串）；产物列表的每一项加
  `note`（这个 server 进程里最近一次交付的说明）。都是不可信的自由文本，界面按文本显示。
- `info.description` 不再带 syndica 的版本号；契约的版本是 `info.version`。
