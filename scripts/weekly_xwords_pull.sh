#!/usr/bin/env bash
set -uo pipefail

[ -f /app/.env.runtime ] && { set -a; source /app/.env.runtime; set +a; }

cd /app/data || exit 1

echo "=== $(date -Is) weekly_xwords_pull.sh starting ==="

cd puzzles || { echo "=== $(date -Is) ERROR: Failed to enter puzzles directory ==="; exit 1; }
git pull || { echo "=== $(date -Is) ERROR: git pull failed ==="; exit 1; }

echo "=== $(date -Is) weekly_xwords_pull.sh done ==="
