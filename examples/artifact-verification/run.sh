#!/bin/sh
# Artifact verification demo: a goal projected into rubrics, a deliverable marked with Deliver, and
# acceptance ending the run with a four-value verdict. The same task runs twice: once with every
# layer on, once with the outcome check (L3) off, which is what `unverified` looks like.
#
#   sh run.sh            use the model in your environment (OPENAI_BASE_URL, OPENAI_MODEL,
#                        OPENAI_API_KEY, or GEMINI_API_KEY / OPENROUTER_API_KEY)
#   sh run.sh --offline  use the scripted fake model in tests/e2e/fake_openai.py (no key, no cost;
#                        needs a clone of the repository)
#
# Needs `syndica` (with the openai extra) and python3 on PATH. Everything is written to a new
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

work=$(mktemp -d "${TMPDIR:-/tmp}/syndica-demo-acceptance.XXXXXX")
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

goal='a release note in notes.md with a Highlights section that lists the breaking change'
task='Write the release note for version 2.0 to notes.md from changes.txt, then deliver it.'

# run <directory> <extra syndica run options...>: one run in a fresh copy of workspace/; prints the
# exit code, which --verdict-exit turns into the verdict.
run() {
  dir=$1
  shift
  cp -R "$here/workspace" "$dir"
  cd "$dir"
  # A run without a terminal cannot answer the trust question: trust this demo's own .syndica.yaml.
  digest=$(syndica config trust </dev/null 2>&1 | sed -n 's/.*--sha256 \([0-9a-f]\{64\}\).*/\1/p' | head -n 1)
  [ -z "$digest" ] || syndica config trust --sha256 "$digest" >/dev/null
  code=0
  syndica run --agent Worker --max-turns 30 --goal "$goal" --verdict-exit "$@" "$task" </dev/null || code=$?
  cd "$work"
  return "$code"
}

verdict() {
  case $1 in
    10) echo pass ;;
    11) echo partial ;;
    12) echo block ;;
    13) echo unverified ;;
    *) echo "no verdict (exit $1)" ;;
  esac
}

echo '== 1. every acceptance layer on'
first=0
run "$work/full" || first=$?
cd "$work/full"
echo '-- syndica artifacts list'
syndica artifacts list
sid=$(syndica sessions list | grep -o '[0-9a-f]\{32\}' | head -n 1)
echo "-- syndica sessions rubrics $sid"
syndica sessions rubrics "$sid"
cd "$work"

echo '== 2. the same task with the outcome check (L3) off'
second=0
run "$work/no-outcome" --flag acceptance_outcome=false || second=$?

echo "== verdicts: with every layer: $(verdict "$first"); with L3 off: $(verdict "$second")"
echo "   workspaces: $work/full and $work/no-outcome"
case $first in 10 | 11 | 12 | 13) ;; *) exit 1 ;; esac
[ "$second" = 13 ] || exit 1
