#!/usr/bin/env bash
set -uo pipefail

[ -f /app/.env.runtime ] && { set -a; source /app/.env.runtime; set +a; }

cd /app/data || exit 1

# Calculate tomorrow's date (already published by 18:10 ET)
TARGET_DATE=$(date -d "+1 day" +%F)

echo "=== $(date -Is) connections_daily.sh starting for ${TARGET_DATE} ==="

python3 /app/scrapers/connections_scraper.py --date "$TARGET_DATE" --output-dir puzzles/connections || { echo "=== ERROR: Connections scraper failed ==="; exit 1; }

echo "=== $(date -Is) connections_daily.sh done ==="
