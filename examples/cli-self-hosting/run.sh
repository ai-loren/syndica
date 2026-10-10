#!/bin/sh
# CLI self-hosting demo: agents fix a failing test in a copy of a tiny project; the host verifies
# the result itself, writes a redacted handoff, and leaves the commit to you.
#
#   sh run.sh            use the model in your environment (OPENAI_BASE_URL, OPENAI_MODEL,
#                        OPENAI_API_KEY, or GEMINI_API_KEY / OPENROUTER_API_KEY)
#   sh run.sh --offline  use the scripted fake model in tests/e2e/fake_openai.py (no key, no cost;
#                        needs a clone of the repository)
#
# Needs `syndica` (with the openai extra), python3 and git on PATH. Everything is written to a new
# temporary directory, printed at the end; your own ~/.config is not touched.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
case ${1:-} in
  '') offline=0 ;;
  --offline) offline=1 ;;
  *)
    echo "expected: no argument or --offline; got: $1" >&2
    exit 2
    ;;
esac

work=$(mktemp -d "${TMPDIR:-/tmp}/syndica-demo-self-hosting.XXXXXX")
cp -R "$here/workspace" "$work/ws"
export XDG_CONFIG_HOME="$work/config"

if [ "$offline" = 1 ]; then
  FAKE_OPENAI_SCRIPT="$here/offline/script.json" "${PYTHON:-python3}" \
    "$here/../../tests/e2e/fake_openai.py" >"$work/fake.port" 2>"$work/fake.log" &
  fake=$!
  trap 'kill "$fake" 2>/dev/null || true' EXIT
  tries=0
  until [ -s "$work/fake.port" ]; do
    tries=$((tries + 1))
    [ "$tries" -le 100 ] || { echo "the fake model did not start; see $work/fake.log" >&2; exit 1; }
    sleep 0.1
  done
  export SYNDICA_LLM_ORDER=openai OPENAI_MODEL=fake OPENAI_API_KEY=offline-demo-key
  OPENAI_BASE_URL="http://127.0.0.1:$(head -n 1 "$work/fake.port")/v1"
  export OPENAI_BASE_URL
fi

cd "$work/ws"
git init -q
git add -A
git -c user.name=demo -c user.email=demo@example.invalid commit -qm 'a tiny project with a failing test'

echo '== the tests before the run'
python3 -m unittest -q 2>&1 | tail -n 1 || true

# A run without a terminal cannot answer the trust question: trust this demo's own .syndica.yaml.
digest=$(syndica config trust </dev/null 2>&1 | sed -n 's/.*--sha256 \([0-9a-f]\{64\}\).*/\1/p' | head -n 1)
[ -z "$digest" ] || syndica config trust --sha256 "$digest"

echo '== syndica run --handoff'
status=0
syndica run --handoff --agent Worker --max-turns 30 \
  'Fix the failing test in this project. Run the tests with python3 -m unittest -q.' </dev/null || status=$?

echo '== what changed (nothing is committed: the host only commits after a person chooses)'
git status --short
git diff --stat

trace=$(syndica trace list | sed -n '1s/^[^ ]* [^ ]*  \([0-9a-f]\{8\}\).*/\1/p')
echo "== syndica trace show $trace"
syndica trace show "$trace"

echo "== done (exit $status). Workspace: $work/ws"
echo "   handoff files: $work/ws/.syndica/handoffs/"
exit "$status"
