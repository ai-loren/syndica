#!/bin/sh
# Training export demo: `syndica batch` runs a dataset of tasks, acceptance gives each one a verdict,
# and `syndica export` turns the sessions into SFT, preference, RL and neutral trajectory files.
# With the trl extra installed, the exported SFT and preference files go through TRL dry runs too.
#
#   sh run.sh            use the model in your environment (OPENAI_BASE_URL, OPENAI_MODEL,
#                        OPENAI_API_KEY, or GEMINI_API_KEY / OPENROUTER_API_KEY)
#   sh run.sh --offline  use the scripted fake model in tests/e2e/fake_openai.py (no key, no cost;
#                        needs a clone of the repository); it scripts the four tasks to end in
#                        pass, block, partial and unverified
#
# Needs `syndica` (with the openai extra) and python3 on PATH; the TRL dry runs use the Python that
# runs `syndica` (override with SYNDICA_PYTHON). Everything is written to a new temporary directory,
# printed at the end; your own ~/.config is not touched.
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

work=$(mktemp -d "${TMPDIR:-/tmp}/syndica-demo-training.XXXXXX")
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
# A run without a terminal cannot answer the trust question: trust this demo's own .syndica.yaml.
digest=$(syndica config trust </dev/null 2>&1 | sed -n 's/.*--sha256 \([0-9a-f]\{64\}\).*/\1/p' | head -n 1)
[ -z "$digest" ] || syndica config trust --sha256 "$digest"

echo "== syndica batch tasks.jsonl"
status=0
syndica batch "$here/tasks.jsonl" </dev/null || status=$?

# Exports must go outside the workspace: they hold other sessions' full text and untrusted content.
for format in sft preference rl trajectory; do
  echo "== syndica export $format"
  syndica export "$format" --output "$work/exports/$format" || status=$?
done

echo '== what each export holds'
python3 - "$work/exports" <<'EOF'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
for name, data in (('sft', 'messages'), ('preference', 'preference'), ('rl', 'rl'), ('trajectory', 'trajectories')):
    manifest = json.loads((root / name / 'manifest.json').read_text(encoding='utf-8'))
    rows = (root / name / f'{data}.jsonl').read_text(encoding='utf-8').splitlines()
    verdicts = sorted(verdict for sample in manifest['samples'] for verdict in sample['verdicts'][-1:])
    print(f'{name:<11} {len(rows)} rows; samples {verdicts}; left out as unverified: {len(manifest["dropped_unverified"])}')
EOF

python=${SYNDICA_PYTHON:-$(sed -n '1s/^#!//p' "$(command -v syndica)")}
if "$python" -c 'import trl, syndica.backends.trl.dry_run' 2>/dev/null; then
  echo '== TRL dry runs (no training, no download)'
  "$python" - "$work/exports" <<'EOF'
import sys
from pathlib import Path

from syndica.backends.trl.dry_run import dry_run, dry_run_preference

root = Path(sys.argv[1])
sft = dry_run(root / 'sft' / 'messages.jsonl')
print(f"SFTTrainer prepared {sft['rows']} rows with columns {sft['prepared_columns']}")
if (root / 'preference' / 'preference.jsonl').read_text(encoding='utf-8').strip():
    preference = dry_run_preference(root / 'preference' / 'preference.jsonl')
    print(f"DPOTrainer prepared {preference['rows']} pairs with columns {preference['prepared_columns']}")
else:
    print('no preference pair: it needs two verdicts of different rank for the same goal and rubrics')
EOF
else
  echo '== TRL dry runs skipped: install syndica[trl] in the environment that runs syndica'
fi

echo "== done (exit $status). Workspace: $work/ws; exports: $work/exports"
exit "$status"
