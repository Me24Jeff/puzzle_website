#!/usr/bin/env bash
set -euo pipefail

# Configure system timezone from the TZ environment variable if set
if [ -n "${TZ:-}" ]; then
  if [ -e "/usr/share/zoneinfo/${TZ}" ]; then
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
  else
    echo "WARNING: unknown TZ '${TZ}', leaving the system timezone alone" >&2
  fi
fi

# cron jobs run in a minimal environment and do NOT inherit the container's
# env vars (NYT_S_COOKIE, XWORDS_REPO_URL, etc). Snapshot them once here to
# a file that every script sources before it runs.
#
# `export -p` prints every exported variable as `declare -x NAME="value"` with
# the value properly quoted/escaped, so cookies or passwords containing spaces,
# $, quotes, ; or & survive the round trip. (The old `export NAME=value` form
# was unquoted and broke on those characters.)
umask 077                                   # file is never world/group readable, not even briefly
export -p \
  | grep -vE '^declare -x (HOME|PWD|OLDPWD|SHLVL|_|TERM|HOSTNAME)=' \
  > /app/.env.runtime.tmp
mv /app/.env.runtime.tmp /app/.env.runtime
chmod 600 /app/.env.runtime

mkdir -p /app/data/logs
LOG=/app/data/logs/cron.log

# Keep the log from growing forever: roll it over once it passes ~5 MB.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5242880 ]; then
  mv -f "$LOG" "$LOG.1"
fi
touch "$LOG"

echo "$(date -Is) cron container starting, timezone: $(cat /etc/timezone 2>/dev/null || echo unknown)" \
  >> "$LOG"

# Foreground cron so the container stays up / logs to `docker logs`.
exec cron -f
