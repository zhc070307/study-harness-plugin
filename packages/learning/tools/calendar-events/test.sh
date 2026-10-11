#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

./build.sh >/dev/null

today=$(date +%F)
tomorrow=$(date -v+1d +%F)
today_json=$(./calendar-events --today | jq -S .)

test "$today_json" = "$(./calendar-events --date "$today" | jq -S .)"
test "$today_json" = "$(./calendar-events --from "$today" --to "$tomorrow" | jq -S .)"
test "$today_json" = "$(./calendar-events --from today --to tomorrow | jq -S .)"

if ./calendar-events --today --days 1 >/dev/null 2>&1; then
  echo "expected conflicting range options to fail" >&2
  exit 1
fi

if ./calendar-events --from "$tomorrow" --to "$today" >/dev/null 2>&1; then
  echo "expected reversed range to fail" >&2
  exit 1
fi

if ./calendar-events --from not-a-date >/dev/null 2>&1; then
  echo "expected malformed range to fail" >&2
  exit 1
fi

echo "calendar-events range tests passed"
