#!/bin/sh
# 前端的门禁：类型生成比对、类型检查、lint、构建、产物哈希。只在开发期需要 Node；用户装的 wheel 里是
# 构建好的产物。前端源码或 OpenAPI 快照有改动的提交都要跑它。
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root/web"

major=$(node -p "process.versions.node.split('.')[0]")
if [ "$major" != "26" ]; then
  echo "期望: Node 26（产物要能逐字节重现）" >&2
  echo "实际: Node $major" >&2
  echo "下一步: 换成 Node 26 再运行 scripts/check_web.sh" >&2
  exit 2
fi

npm ci --no-audit --no-fund --loglevel=error
before=$(cat src/generated/openapi.ts src/generated/events.ts | shasum -a 256)
npm run --silent gen
after=$(cat src/generated/openapi.ts src/generated/events.ts | shasum -a 256)
if [ "$before" != "$after" ]; then
  echo "期望: web/src/generated 与 docs/reference/openapi.json 一致" >&2
  echo "实际: 重新生成之后类型变了" >&2
  echo "下一步: 提交重新生成的 web/src/generated，并确认前端代码跟着改了" >&2
  exit 1
fi
npm run --silent typecheck
npm run --silent lint
npm run --silent build
cd "$root"
uv run python scripts/web_build_info.py --write
echo "check_web: ok"
