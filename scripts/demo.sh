#!/usr/bin/env bash
# Run the reproducible, offline Nimbus demo: no network call, no agent call,
# identical output every time. This is the command to run in the room.
#
# Usage: ./scripts/demo.sh
#
# Runs `uv run nimbus demo --offline` and, if `uv` is installed, additionally
# re-runs it a second time and diffs the two outputs to prove the "identical
# every run" claim live, rather than just asserting it.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required (https://docs.astral.sh/uv/) -- not found on PATH." >&2
    exit 1
fi

echo "Run 1:"
uv run nimbus demo --offline | tee /tmp/nimbus-demo-run1.txt

echo
echo "Run 2 (proving the output above is reproducible, not just plausible):"
uv run nimbus demo --offline | tee /tmp/nimbus-demo-run2.txt

if diff -q /tmp/nimbus-demo-run1.txt /tmp/nimbus-demo-run2.txt >/dev/null; then
    echo
    echo "Both runs produced byte-identical output."
else
    echo
    echo "Runs differed -- this should never happen for --offline. See the diff below." >&2
    diff /tmp/nimbus-demo-run1.txt /tmp/nimbus-demo-run2.txt >&2
    exit 1
fi
