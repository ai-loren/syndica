#!/bin/sh
# Web multi-agent demo: `syndica up` on a copy of workspace/. In the web UI, an Orchestrator session
# delegates the task in prompt.txt to the Worker, the Worker's copy out of the workspace waits for
# your approval, and the report it wrote can be previewed as an artifact.
#
#   sh run.sh            use the model in your environment (OPENAI_BASE_URL, OPENAI_MODEL,
#                        OPENAI_API_KEY, or GEMINI_API_KEY / OPENROUTER_API_KEY)
#   sh run.sh --offline  use the scripted fake model in tests/e2e/fake_openai.py (no key, no cost;
#                        needs a clone of the repository)
#
# Needs `syndica` with the server and openai extras, and python3, on PATH. The server runs in the
# foreground until Ctrl-C. Everything is written to a new temporary directory, printed below; your
# own ~/.config is not touched.
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

work=$(mktemp -d "${TMPDIR:-/tmp}/syndica-demo-web.XXXXXX")
cp -R "$here/workspace" "$work/ws"
mkdir "$work/outbox"
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
# Trust this demo's own .syndica.yaml up front, so the server starts without a question.
digest=$(syndica config trust </dev/null 2>&1 | sed -n 's/.*--sha256 \([0-9a-f]\{64\}\).*/\1/p' | head -n 1)
[ -z "$digest" ] || syndica config trust --sha256 "$digest"

cat <<EOF
== In the page that opens:
   1. pick the role Orchestrator and click 新会话 (new session);
   2. send this message:
      $(cat "$here/prompt.txt")
   3. approve the card that asks about \`cp incident.md ../outbox/incident.md\` (批准 = approve);
   4. open the 产物 (artifacts) tab and click incident.md.
   Workspace: $work/ws; the approved copy lands in $work/outbox. Ctrl-C stops the server.
EOF
syndica up --foreground
