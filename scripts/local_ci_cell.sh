#!/bin/sh
# scripts/local_ci.sh 的一格：把导出的提交拷进工作目录、做成只有一次提交的 git 仓库（CI 跑在检出的仓库
# 里，check_links 与 check_web_reproduced 都按 git 认文件），然后跑 .github/workflows/ci.yml 里对应
# job 的步骤。下面 gate / gate_in 的每一行与 ci.yml 的 run 步骤逐条一致，tests/test_ci_workflow.py
# 守着这一点：改了一边就要改另一边。
#
# 用法：local_ci_cell.sh <导出目录> <工作目录> gates <torch: yes|no> <pyright: yes|no>
#       local_ci_cell.sh <导出目录> <工作目录> web|package|security
set -u

usage() {
  echo "期望: local_ci_cell.sh <导出目录> <工作目录> gates <yes|no> <yes|no>，或 ... web|package|security" >&2
  echo "实际: $*" >&2
  echo "下一步: 经 scripts/local_ci.sh 运行，它会给出这些参数" >&2
  exit 2
}

[ $# -ge 3 ] || usage "$@"
export_dir=$1
work=$2
job=$3
shift 3

rm -rf "$work"
mkdir -p "$work"
cp -R "$export_dir/." "$work/"
chmod -R u+w "$work"
cd "$work" || exit 2
git init --quiet
git add --all --force
git -c user.name=local-ci -c user.email=local-ci@invalid -c commit.gpgsign=false \
  -c trace2.eventtarget= -c trace2.eventnesting= -c core.hooksPath=/dev/null \
  commit --quiet --no-verify --message 'local ci export'

status=0
gate() {
  started=$(date +%s)
  echo "=== $*"
  if "$@"; then
    echo "=== ok in $(($(date +%s) - started))s: $*"
  else
    echo "=== FAILED in $(($(date +%s) - started))s: $*"
    status=1
  fi
}
gate_in() {
  directory=$1
  shift
  started=$(date +%s)
  echo "=== ($directory) $*"
  if (cd "$directory" && "$@"); then
    echo "=== ok in $(($(date +%s) - started))s: ($directory) $*"
  else
    echo "=== FAILED in $(($(date +%s) - started))s: ($directory) $*"
    status=1
  fi
}
# 环境没装上，后面每一道都只会以同一个原因失败。
stop_unless_installed() {
  if [ "$status" != 0 ]; then
    exit 1
  fi
}

case "$job" in
gates)
  [ $# -eq 2 ] || usage "$job" "$@"
  trl=$1
  pyright=$2
  if [ "$trl" = no ]; then
    gate uv sync --locked --all-extras --no-extra trl
  fi
  if [ "$trl" = yes ]; then
    gate uv sync --locked --all-extras
  fi
  stop_unless_installed
  gate uv run ruff format --check .
  gate uv run ruff check .
  if [ "$pyright" = yes ]; then
    gate uv run pyright
  fi
  gate uv run python scripts/check_deps.py
  gate uv run python scripts/check_links.py
  gate uv run pytest -q
  ;;
web)
  gate uv sync --locked --all-extras --no-extra trl
  stop_unless_installed
  gate_in web npm ci --no-audit --no-fund
  gate_in web npx playwright install --with-deps chromium
  gate scripts/check_e2e.sh
  gate scripts/check_web_reproduced.sh
  ;;
package)
  gate uv build
  gate uv tool run twine==7.0.0 check --strict dist/*
  ;;
security)
  gate scripts/audit_deps.sh
  ;;
*)
  usage "$job" "$@"
  ;;
esac
exit "$status"
