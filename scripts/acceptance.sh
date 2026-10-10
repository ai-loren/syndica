#!/bin/sh
# 发布验收：从构建出的 wheel（uv build 到私有目录，再过 scripts/check_dist.py）跑路线图 M12 的四项 E2E，
# 模型是脚本化的假 OpenAI 端点（tests/e2e/fake_openai.py 的拷贝，测试夹具，不属于产品）：
#
#   (a) 干净的 Linux 容器里照 README 的安装与上手命令走到一次成功的 syndica run，五分钟之内；
#   (b) Web UI：Orchestrator 派活给 Worker、页面上批准一次确认、打开产物预览（Playwright）；
#   (c) OpenAI SDK 的客户端工具往返与 MCP SDK 的列工具 / Read，连 (b) 那个 syndica up --api-token；
#   (d) syndica batch 得出四种裁决，syndica export 四种格式，查计数、unverified 排除与密钥不外泄。
#
# 逐项打 PASS / FAIL 与用时，有一项失败就退出 1。环境里有 OPENAI_API_KEY + OPENAI_BASE_URL +
# OPENAI_MODEL（或 GEMINI_API_KEY）时，(a) 与 (d) 再对真模型各跑一次；key 不写盘、不进日志。
#
# 用法：sh scripts/acceptance.sh [--only a,b,c,d] [--dist DIR] [--out DIR] [--python 3.12] [--image IMAGE]
# 需要：uv、docker（本机是 Colima）、Node 与 web/ 下的 Playwright（缺 node_modules 时自动 npm ci）、
#       能访问 PyPI（容器里照 README 用 pip 装依赖）。
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
for tool in uv docker npx; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "期望: PATH 里有 $tool" >&2
    echo "实际: 找不到它" >&2
    echo "下一步: 装上 $tool（docker 在本机由 colima start 提供）后重跑" >&2
    exit 2
  fi
done
if ! docker info >/dev/null 2>&1; then
  echo "期望: docker 守护进程在跑" >&2
  echo "实际: docker info 失败" >&2
  echo "下一步: colima start（或启动 Docker Desktop）后重跑" >&2
  exit 2
fi
exec uv run --no-project --python 3.12 python "$root/scripts/acceptance_run.py" "$@"
