#!/bin/sh
# 在 scripts/check_web.sh 之后运行：它重新生成类型、重新构建并重写 build-info.json，提交里的这几样必须与
# 这次构建逐字节相同，否则随 wheel 发出去的前端不是这份源码构建出来的。要在 git 工作区里跑。
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
changed=$(git status --porcelain --untracked-files=all -- src/syndica/server/static web/src/generated)
if [ -n "$changed" ]; then
  echo "期望: 重新构建出的前端产物与生成的类型和提交里的逐字节相同" >&2
  echo "实际: 构建之后这些文件变了：" >&2
  printf '%s\n' "$changed" >&2
  echo "下一步: 用 Node 26 在本地跑 scripts/check_web.sh，把变化的文件一起提交；本地构建与这里不同，" \
    "就比较两边的 node -v 与 npm ci 装上的版本" >&2
  exit 1
fi
echo "check_web_reproduced: ok"
