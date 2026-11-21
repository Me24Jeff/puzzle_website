#!/usr/bin/env bash
set -uo pipefail

[ -f /app/.env.runtime ] && { set -a; source /app/.env.runtime; set +a; }

cd /app/data || exit 1

# Calculate tomorrow's date (standard for Linux/Docker containers)
TARGET_DATE=$(date -d "+1 day" +%F)

echo "=== $(date -Is) crosswords_daily.sh starting for ${TARGET_DATE} ==="
if [ -z "${NYT_S_COOKIE:-}" ]; then
    echo "  ! NYT_S_COOKIE not set - skipping crossword scrape (needs an NYT Games subscription cookie)."
    exit 1
else
    python3 /app/scrapers/crossword_scraper.py --type all --date "$TARGET_DATE" --output-dir puzzles/crosswords || { echo "=== ERROR: Crossword scraper failed ==="; exit 1; }
fi

echo "=== $(date -Is) crosswords_daily.sh done ==="
