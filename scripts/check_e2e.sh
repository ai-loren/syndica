#!/bin/sh
# 浏览器 E2E：起真的 `syndica up`（模型指向脚本化的 OpenAI 兼容假端点，不需要 key），桌面与移动视口各跑
# 一遍。需要 Node 26 与 Playwright 缓存里的 Chromium；先构建前端，测的就是随包发布的那份产物。
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
"$root/scripts/check_web.sh"
cd "$root/web"
npx playwright test --reporter=line
