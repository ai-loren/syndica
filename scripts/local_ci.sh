#!/bin/sh
# 在本机跑 CI 的同一套门禁：macOS 上的 Python 3.11 / 3.12 / 3.13、Docker（Colima）里 Linux 的
# 3.11 / 3.12 / 3.13，再加前端、打包与依赖扫描。跑的是 git archive 导出的提交，不是工作区：没提交的
# 改动不算数。每一格各拷一份导出、各建自己的环境；Linux 容器只读挂载导出，再拷进容器里跑。
#
# 用法：scripts/local_ci.sh [--jobs 1|2] [--cells "<格> ..."] [<提交>]
#   <提交>   默认 HEAD。
#   --jobs   默认 1：一格接一格。2：本机一路、Linux 容器一路同时跑。再多就把机器占满，对时间敏感的
#            用例开始随机失败。
#   --cells  只跑这几格；格名见下面的 MATRIX 与 EXTRA_CELLS。
# 日志与各格的工作目录在 ${LOCAL_CI_OUT:-$HOME/.cache/syndica-local-ci}/<提交>/ 下，同一个提交重跑时
# 先清掉。默认放在 HOME 下：Colima 只把 HOME 挂进虚拟机，放在 /tmp 下的导出容器里看不见。
set -u

UV_VERSION=0.12.19
# 格名、系统、Python、装不装 torch（trl extra）、跑不跑 pyright：与 ci.yml 的 gates 矩阵逐格对应，
# tests/test_ci_workflow.py 守着这一点。
MATRIX='
mac-3.11 macos 3.11 no no
mac-3.12 macos 3.12 no no
mac-3.13 macos 3.13 yes no
linux-3.11 linux 3.11 no yes
linux-3.12 linux 3.12 no no
linux-3.13 linux 3.13 no no
'
EXTRA_CELLS='web package security'

fail_usage() {
  echo "期望: $1" >&2
  echo "实际: $2" >&2
  echo "下一步: $3" >&2
  exit 2
}

here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
jobs=1
cells=''
commit=HEAD
while [ $# -gt 0 ]; do
  case "$1" in
  --jobs)
    [ $# -ge 2 ] || fail_usage '--jobs 后面跟 1 或 2' '--jobs 后面什么都没有' '写成 --jobs 1 或 --jobs 2'
    jobs=$2
    shift 2
    ;;
  --cells)
    [ $# -ge 2 ] || fail_usage '--cells 后面跟用空格分开的格名' '--cells 后面什么都没有' \
      "从这些里挑：$(echo "$MATRIX" | awk 'NF { printf "%s ", $1 }')$EXTRA_CELLS"
    cells=$2
    shift 2
    ;;
  -*)
    fail_usage '--jobs、--cells 或一个提交' "未知选项 $1" '看 scripts/local_ci.sh 开头的用法'
    ;;
  *)
    commit=$1
    shift
    ;;
  esac
done
case "$jobs" in
1 | 2) ;;
*) fail_usage '--jobs 是 1 或 2' "--jobs $jobs" '同时跑更多格会让对时间敏感的用例随机失败；用 1 或 2' ;;
esac

known="$(echo "$MATRIX" | awk 'NF { printf "%s ", $1 }')$EXTRA_CELLS"
if [ -z "$cells" ]; then
  cells=$known
fi
for cell in $cells; do
  case " $known " in
  *" $cell "*) ;;
  *) fail_usage "格名是这些之一：$known" "--cells 里有 $cell" '改成上面列出的格名' ;;
  esac
done

sha=$(git -C "$repo" rev-parse --verify --quiet "$commit^{commit}") ||
  fail_usage '一个存在的提交（分支名、标签或哈希）' "$commit" '用 git log 找到要验证的提交再传进来'
short=$(echo "$sha" | cut -c1-12)
out="${LOCAL_CI_OUT:-$HOME/.cache/syndica-local-ci}/$short"
host_uv=$(uv --version 2>/dev/null | awk '{ print $2 }')
if [ "$host_uv" != "$UV_VERSION" ]; then
  echo "注意: 本机 uv 是 ${host_uv:-没装}，CI 钉的是 ${UV_VERSION}；本机各格用本机的 uv，结论可能与 CI 不同。" >&2
fi

if [ -d "$out" ]; then
  chmod -R u+w "$out" 2>/dev/null
  rm -rf "$out"
fi
mkdir -p "$out/export" "$out/harness" "$out/results"
git -C "$repo" archive "$sha" | tar -x -C "$out/export" ||
  fail_usage "能把 $short 导出到 $out/export" 'git archive 或 tar 失败' '确认磁盘空间与目录权限'
cp "$here/local_ci_cell.sh" "$out/harness/"
echo "提交 ${short}，输出在 $out"

duration() {
  printf '%dm%02ds' $(($1 / 60)) $(($1 % 60))
}

# 一格的结论：PASS/FAIL、耗时、测试数、日志。一格一行，同时跑的两路不会把一行拆开。
record() {
  cell=$1
  code=$2
  seconds=$3
  log=$4
  counts=$(grep -E '^[0-9]+ (passed|failed)|^ *[0-9]+ (passed|failed) \(' "$log" | tail -1 | sed 's/^ *//')
  verdict=PASS
  [ "$code" = 0 ] || verdict=FAIL
  line="$verdict $cell $(duration "$seconds")${counts:+ [$counts]} log: $log"
  echo "$line" > "$out/results/$cell"
  echo "$line"
}

host_env() {
  env -i HOME="$HOME" PATH="$PATH" TMPDIR="${TMPDIR:-/tmp}" USER="${USER:-}" LOGNAME="${LOGNAME:-}" \
    LANG="${LANG:-en_US.UTF-8}" CI=true "$@"
}

run_host() {
  cell=$1
  python=$2
  shift 2
  log="$out/$cell.log"
  started=$(date +%s)
  host_env UV_PYTHON="$python" sh "$out/harness/local_ci_cell.sh" "$out/export" "$out/$cell" "$@" \
    > "$log" 2>&1
  record "$cell" $? $(($(date +%s) - started)) "$log"
}

image_for() {
  echo "syndica-local-ci:py$1-uv$UV_VERSION"
}

build_image() {
  python=$1
  log=$2
  # python:3.X-bookworm 是带 git、procps 的完整 Debian，与托管 runner 一样以非 root 用户跑：
  # 不少用例在 root 下跳过（root 不受文件权限约束）。
  docker build --tag "$(image_for "$python")" --build-arg PYTHON="$python" - > "$log" 2>&1 <<EOF
ARG PYTHON
FROM python:\${PYTHON}-bookworm
COPY --from=ghcr.io/astral-sh/uv:$UV_VERSION /uv /uvx /usr/local/bin/
RUN command -v git ps \
 && useradd --create-home --uid 1001 ci \
 && mkdir -p /home/ci/.cache/uv \
 && chown -R ci:ci /home/ci/.cache
USER ci
WORKDIR /home/ci
EOF
}

run_linux() {
  cell=$1
  python=$2
  shift 2
  log="$out/$cell.log"
  started=$(date +%s)
  if ! docker info > /dev/null 2>&1; then
    {
      echo "期望: Docker 可用（本机是 Colima）"
      echo "实际: docker info 失败"
      echo "下一步: colima start，再重跑这一格：scripts/local_ci.sh --cells $cell $short"
    } > "$log"
    record "$cell" 1 0 "$log"
    return
  fi
  if ! build_image "$python" "$out/$cell.image.log"; then
    cp "$out/$cell.image.log" "$log"
    record "$cell" 1 $(($(date +%s) - started)) "$log"
    return
  fi
  # --init：容器里的 1 号进程要收养并回收孤儿进程，否则被杀掉的进程组留成僵尸，判「进程不在了」的
  # 用例看到的是一直活着的进程号。
  docker run --rm --init \
    --volume "$out/export:/export:ro" \
    --volume "$out/harness:/harness:ro" \
    --volume syndica-local-ci-uv-cache:/home/ci/.cache/uv \
    --env CI=true \
    --env UV_PYTHON="$python" \
    --env UV_PYTHON_DOWNLOADS=never \
    --env UV_LINK_MODE=copy \
    "$(image_for "$python")" \
    sh /harness/local_ci_cell.sh /export /home/ci/work "$@" > "$log" 2>&1
  record "$cell" $? $(($(date +%s) - started)) "$log"
}

selected() {
  case " $cells " in
  *" $1 "*) return 0 ;;
  *) return 1 ;;
  esac
}

lane_host() {
  echo "$MATRIX" | while read -r cell system python trl pyright; do
    if [ "$system" = macos ] && selected "$cell"; then
      run_host "$cell" "$python" gates "$trl" "$pyright"
    fi
  done
  for cell in $EXTRA_CELLS; do
    if selected "$cell"; then
      run_host "$cell" 3.11 "$cell"
    fi
  done
}

lane_linux() {
  echo "$MATRIX" | while read -r cell system python trl pyright; do
    if [ "$system" = linux ] && selected "$cell"; then
      run_linux "$cell" "$python" gates "$trl" "$pyright"
    fi
  done
}

run_started=$(date +%s)
if [ "$jobs" = 2 ]; then
  lane_host &
  lane_linux &
  wait
else
  lane_host
  lane_linux
fi

echo
echo "== 提交 ${short}，共 $(duration $(($(date +%s) - run_started)))"
status=0
for cell in $cells; do
  if [ -f "$out/results/$cell" ]; then
    cat "$out/results/$cell"
    grep -q '^PASS ' "$out/results/$cell" || status=1
  else
    echo "FAIL $cell 没有结论（被中断？）"
    status=1
  fi
done
exit "$status"
