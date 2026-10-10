#!/bin/sh
# 依赖安全扫描：uv.lock 里的每一个包（全部 extras、每个平台、每个 Python 版本各自锁定的那份）过
# pip-audit，web/package-lock.json 过 npm audit（high 及以上算失败）。要联网。CI 的 security job
# 与 scripts/local_ci.sh 跑的都是它。
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

uv export --project "$root" --locked --all-extras --no-emit-project --no-hashes --no-annotate \
  --no-header --format requirements.txt --quiet > "$work/locked.txt"
# pip-audit 按它自己的解释器求值环境标记：留着标记，只在 Linux、Windows 或别的 Python 版本上才装的包
# 会被静默跳过。去掉标记之后同名不同版本的包（numpy 按 Python 版本锁了两个）又不能进同一个文件，
# 所以同名的第 n 个版本进第 n 个文件。
sed 's/[[:space:]]*;.*$//' "$work/locked.txt" | sort -u |
  awk -F'==' -v dir="$work" '{ n[$1]++; print > (dir "/requirements-" n[$1] ".txt") }'

status=0
for requirements in "$work"/requirements-*.txt; do
  echo "pip-audit: $(wc -l < "$requirements" | tr -d ' ') 个包"
  uv tool run pip-audit==2.10.1 --disable-pip --no-deps --strict --progress-spinner off \
    --requirement "$requirements" || status=1
done
echo "npm audit: web/package-lock.json"
(cd "$root/web" && npm audit --audit-level=high) || status=1

if [ "$status" != 0 ]; then
  echo "期望: 锁定的依赖里没有已知漏洞（npm 只算 high 及以上）" >&2
  echo "实际: 上面列出了有漏洞的包" >&2
  echo "下一步: 能升就升（uv lock --upgrade-package <包> / 在 web 里 npm update <包>）并让门禁保持全绿；" \
    "不适用的写进 docs/audits 里当期的安全扫描记录并说明理由" >&2
fi
exit "$status"
