#!/bin/sh
# 文档站的门禁：按发布时的配置把 docs/user 构建到一个用完即删的临时目录。--strict 把 MkDocs 的每条
# 警告（断链、锚点不存在、页面没进导航）都变成失败；站点不落在仓库里，免得构建产物被当成文档提交。
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

if ! uv run --extra docs python -c 'import mkdocs, material' 2>/dev/null; then
  echo "期望: 装好了文档站的依赖（mkdocs、mkdocs-material）" >&2
  echo "实际: 当前环境 import 不到它们" >&2
  echo "下一步: uv sync --extra docs（或 uv sync --all-extras）之后再运行 scripts/check_docs.sh" >&2
  exit 2
fi

site=$(mktemp -d)
trap 'rm -rf "$site"' EXIT
uv run --extra docs mkdocs build --strict --site-dir "$site"
