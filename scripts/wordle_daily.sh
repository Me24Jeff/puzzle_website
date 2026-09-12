#!/usr/bin/env bash
set -uo pipefail

[ -f /app/.env.runtime ] && { set -a; source /app/.env.runtime; set +a; }

cd /app/data || exit 1

# Calculate tomorrow's date
TARGET_DATE=$(date -d "+1 day" +%F)

echo "=== $(date -Is) wordle_daily.sh starting for ${TARGET_DATE} ==="

python3 /app/scrapers/wordle_scraper.py --date "$TARGET_DATE" --output puzzles/wordle_words.txt || { echo "=== ERROR: Wordle scraper failed ==="; exit 1; }

echo "=== $(date -Is) wordle_daily.sh done ==="
