#!/usr/bin/env bash
set -uo pipefail

[ -f /app/.env.runtime ] && { set -a; source /app/.env.runtime; set +a; }

cd /app/data || exit 1

echo "=== $(date -Is) wordle_daily.sh starting ==="

python3 /app/scrapers/wordle_scraper.py --output puzzles/wordle_words.txt || { echo "=== $(date -Is) ERROR: Wordle scraper failed ==="; exit 1; }

echo "=== $(date -Is) wordle_daily.sh done ==="
