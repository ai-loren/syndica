#!/bin/sh
# 从 dist/ 里构建好的 wheel 往干净的 venv 里装 syndica：基础包与每个 extra 各一个 venv，各自跑
# syndica --version、syndica --help、uv pip check 与这个 extra 的导入检查。
#
# extra 的导入检查同时导入 backend 模块与它要的 SDK：好几个 backend 在函数体里才导入 SDK，只导入
# 模块的话没装 extra 也能过。基础 venv 里反过来要求每一条 extra 的检查都失败，证明它们不是空检查。
#
# trl 缺省不跑：它带上 torch（Linux 上还有 CUDA 的几 GB 依赖），比其余目标加起来还慢、还大。
# 发布前用 --with-trl 跑一次。
#
# 用法：sh scripts/smoke_install.sh [--with-trl]
# 环境变量：SMOKE_PYTHON（venv 的 Python，缺省 3.11，即 requires-python 的下限）、
#           SMOKE_KEEP=1（留下 venv 以便排查）。
# 退出码：0 全部通过；1 有目标失败（逐条报期望、实际、下一步）；2 用法错误或找不到 wheel。
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
python=${SMOKE_PYTHON:-3.11}
with_trl=0
for arg in "$@"; do
  case $arg in
    --with-trl) with_trl=1 ;;
    -h | --help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "期望: 参数只有 --with-trl" >&2
      echo "实际: 收到 $arg" >&2
      echo "下一步: 去掉它；Python 版本用 SMOKE_PYTHON=3.12 这样的环境变量指定" >&2
      exit 2
      ;;
  esac
done

set -- "$root"/dist/syndica-*-py3-none-any.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "期望: dist/ 里恰好有一个 syndica-<版本>-py3-none-any.whl" >&2
  echo "实际: 找到 $# 个匹配（$*）" >&2
  echo "下一步: 删掉 dist/ 后运行 uv build，再跑 uv run python scripts/check_dist.py" >&2
  exit 2
fi
wheel=$1
version=$(basename "$wheel" | sed 's/^syndica-//; s/-py3-none-any\.whl$//')

work=$(mktemp -d "${TMPDIR:-/tmp}/syndica-smoke.XXXXXX")
# 物理路径：macOS 的 /var 是指向 /private/var 的符号链接，Python 报的是解析之后的路径。
work=$(cd "$work" && pwd -P)
if [ "${SMOKE_KEEP:-0}" = 1 ]; then
  echo "venv 留在 $work"
else
  trap 'rm -rf "$work"' EXIT
fi
# 检查在临时目录里跑、不带 PYTHONPATH 与用户 site：导入的必须是装进 venv 的那一份，不是工作区里的源码。
cd "$work"
unset PYTHONPATH VIRTUAL_ENV
export PYTHONNOUSERSITE=1

imports_for() {
  case $1 in
    openai) echo 'import httpx; import syndica.backends.llm_openai.client' ;;
    otel) echo 'import httpx; import syndica.backends.otel_otlp.exporter' ;;
    server) echo 'import starlette, uvicorn, websockets; import syndica.server.app, syndica.serve' ;;
    mcp) echo 'import mcp, jsonschema; import syndica.backends.mcp.client, syndica.backends.mcp.tools' ;;
    acp) echo 'import acp; import syndica.backends.acp.model, syndica.backends.acp.probe' ;;
    a2a) echo 'import a2a.server, httpx; import syndica.backends.a2a.model' ;;
    trl) echo 'import trl, datasets, transformers, tokenizers; from syndica.backends.trl import dry_run' ;;
  esac
}

base_check='
import importlib.resources
import syndica, syndica.kernel, syndica.cli.main, syndica.runner
package = importlib.resources.files("syndica")
for relative in ("py.typed", "server/static/index.html", "server/static/build-info.json"):
    assert (package / relative).is_file(), f"装好的包里没有 {relative}"
'

failures=0
log="$work/step.log"

# fail <目标> <期望> <下一步>：实际取自刚才那一步的输出。
fail() {
  echo "[$1] 期望: $2" >&2
  echo "[$1] 实际: $(tail -n 5 "$log" | tr '\n' ' ')" >&2
  echo "[$1] 下一步: $3" >&2
}

smoke() {
  target=$1
  venv="$work/$target"
  bin="$venv/bin"
  spec=$wheel
  [ "$target" = base ] || spec="$wheel[$target]"
  if ! uv venv --quiet --python "$python" "$venv" >"$log" 2>&1; then
    fail "$target" "uv venv 用 Python $python 建出 venv" "uv python install $python，或用 SMOKE_PYTHON 换一个版本"
    return 1
  fi
  # README 让用户带 --pre 装 rc：pip 的 --pre 对每个依赖都生效，预发布的依赖会被装进来。
  # 这里照同样的方式装，依赖的预发布版先在这里撞上，而不是在用户那里。
  if ! uv pip install --quiet --prerelease allow --python "$bin/python" "$spec" >"$log" 2>&1; then
    fail "$target" "uv pip install '$spec' 成功" "看上面的解析错误，修 [project.optional-dependencies] 后重新 uv build"
    return 1
  fi
  if ! uv pip check --python "$bin/python" >"$log" 2>&1; then
    fail "$target" "装好的依赖互相兼容（uv pip check）" "收紧或放宽 pyproject.toml 里冲突的那条版本约束"
    return 1
  fi
  if ! "$bin/syndica" --version >"$log" 2>&1 || [ "$(cat "$log")" != "syndica $version" ]; then
    fail "$target" "syndica --version 打印 syndica $version" "确认 syndica.__version__ 读的是发行版元数据，然后重新 uv build"
    return 1
  fi
  if ! "$bin/syndica" --help >"$log" 2>&1 || ! grep -q '^usage: syndica' "$log"; then
    fail "$target" "syndica --help 打印用法并退出 0" "在源码树里跑 uv run syndica --help 复现"
    return 1
  fi
  "$bin/python" -c 'import os, syndica; print(os.path.realpath(syndica.__file__))' >"$log" 2>&1 || true
  case $(cat "$log") in
    "$venv"/*) ;;
    *)
      fail "$target" "syndica 从 $venv 里导入" "去掉指向源码树的 PYTHONPATH 或 .pth 后重跑"
      return 1
      ;;
  esac
  if [ "$target" = base ]; then
    if ! "$bin/python" -c "$base_check" >"$log" 2>&1; then
      fail base "基础包能导入，且带着 py.typed 与前端产物" "跑 uv run python scripts/check_dist.py 看 wheel 里缺什么"
      return 1
    fi
    for extra in openai otel server mcp acp a2a trl; do
      if "$bin/python" -c "$(imports_for "$extra")" >"$log" 2>&1 \
        || ! grep -q ModuleNotFoundError "$log"; then
        echo "[base] 期望: 没装 $extra 时它的导入检查以 ModuleNotFoundError 失败" >&2
        echo "[base] 实际: $(tail -n 1 "$log")" >&2
        echo "[base] 下一步: 基础依赖把 $extra 的 SDK 带进来了，或者 imports_for 里那条检查是空的；改其中一处" >&2
        return 1
      fi
    done
  elif ! "$bin/python" -c "$(imports_for "$target")" >"$log" 2>&1; then
    fail "$target" "装了 syndica[$target] 之后 $(imports_for "$target") 成功" "把缺的包加进 [project.optional-dependencies] 的 $target"
    return 1
  fi
  return 0
}

targets="base openai server mcp acp a2a otel"
[ "$with_trl" = 1 ] && targets="$targets trl"
echo "wheel: $(basename "$wheel")；Python $python"
for target in $targets; do
  start=$(date +%s)
  if smoke "$target"; then
    status=ok
  else
    status=FAILED
    failures=$((failures + 1))
  fi
  seconds=$(($(date +%s) - start))
  size=$(du -sk "$work/$target" 2>/dev/null | cut -f1)
  printf '%-8s %-7s %4ss %6s MB\n' "$target" "$status" "$seconds" "$((${size:-0} / 1024))"
done

if [ "$failures" -ne 0 ]; then
  echo "smoke_install: $failures 个目标失败" >&2
  exit 1
fi
echo "smoke_install: ok"
