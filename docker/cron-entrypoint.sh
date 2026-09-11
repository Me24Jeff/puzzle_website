#!/usr/bin/env bash
set -euo pipefail

# cron jobs run in a minimal environment and do NOT inherit the container's
# env vars (NYT_S_COOKIE, XWORDS_REPO_URL, etc). Snapshot them once here to
# a file that every script sources before it runs.
printenv | sed -e 's/^\(.*\)$/export \1/' \
  | grep -vE '^export (HOME|PWD|SHLVL|_)=' > /app/.env.runtime
chmod 600 /app/.env.runtime

mkdir -p /app/data/logs
touch /app/data/logs/cron.log

echo "$(date -Is) cron container starting, timezone: $(cat /etc/timezone 2>/dev/null || echo unknown)" \
  >> /app/data/logs/cron.log

# Foreground cron so the container stays up / logs to `docker logs`.
cron -f
