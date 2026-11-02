#!/usr/bin/env bash
# Reproduce the README demo with the synthetic fixtures in this repository.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO="${T2S_DEMO_DIR:-$ROOT/.demo}"
WS="$DEMO/.thread2skill"
PROJECT="$DEMO/project"

rm -rf "$DEMO"
mkdir -p "$PROJECT"

t2s --home "$WS" init --provider offline

ids=""
for path in \
  "$ROOT/tests/fixtures/slack-export" \
  "$ROOT/tests/fixtures/discord/incidents.json" \
  "$ROOT/tests/fixtures/whatsapp/release-checklist.txt" \
  "$ROOT/tests/fixtures/generic/threads.jsonl"
do
  payload="$(t2s --home "$WS" ingest "$path" --json)"
  batch="$(printf '%s' "$payload" | python -c 'import json,sys; print(" ".join(item["id"] for item in json.load(sys.stdin)["threads"]))')"
  ids="$ids $batch"
done

for id in $ids; do
  t2s --home "$WS" distill "$id" --provider offline
done

slug="$(t2s --home "$WS" list --json | python -c 'import json,sys; print(json.load(sys.stdin)[0]["slug"])')"

t2s --home "$WS" list
t2s --home "$WS" suggest "integration tests are flaky with postgres"
t2s --home "$WS" validate --all
t2s --home "$WS" install "$slug" --harness claude,agents --scope project --project-root "$PROJECT"
t2s --home "$WS" hooks install --harness claude,cursor,deepseek --scope project --project-root "$PROJECT"
t2s --home "$WS" feedback "$slug" --outcome worked

echo
echo "Demo workspace: $WS"
echo "Installed skills: $PROJECT/.claude/skills"