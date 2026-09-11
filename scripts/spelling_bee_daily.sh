#!/usr/bin/env bash
set -uo pipefail

[ -f /app/.env.runtime ] && { set -a; source /app/.env.runtime; set +a; }

cd /app/data || exit 1

echo "=== $(date -Is) spelling_bee_daily.sh starting ==="

python3 /app/scrapers/spelling_bee_scraper.py --output-dir puzzles/spelling-bee || { echo "=== ERROR: Spelling-Bee scraper failed ==="; exit 1; }

echo "=== $(date -Is) spelling_bee_daily.sh done ==="
