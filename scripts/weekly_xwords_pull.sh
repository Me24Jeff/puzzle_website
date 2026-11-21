#!/usr/bin/env bash
set -uo pipefail

[ -f /app/.env.runtime ] && { set -a; source /app/.env.runtime; set +a; }

cd /app/data || exit 1

# Cron has no terminal: fail fast instead of hanging forever on a credential prompt.
export GIT_TERMINAL_PROMPT=0

echo "=== $(date -Is) weekly_xwords_pull.sh starting ==="

cd puzzles || { echo "=== ERROR: Failed to enter puzzles directory ==="; exit 1; }
git pull --ff-only || { echo "=== ERROR: git pull failed ==="; exit 1; }

echo "=== $(date -Is) weekly_xwords_pull.sh done ==="
