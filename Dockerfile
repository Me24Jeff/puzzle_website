FROM python:3.12-slim

# cron        -> runs the scrapers on a schedule (used by the "scraper" service)
# git, rsync  -> weekly pull of the xwords archive from GitHub
# tzdata      -> so cron fires at the right wall-clock time (NYT resets on US/Eastern)
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron git rsync tzdata \
    && rm -rf /var/lib/apt/lists/*

# Unbuffered output so scraper/gunicorn lines show up in `docker logs` and
# cron.log immediately and in order; no .pyc files written into the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY sw.js .
COPY templates/ templates/
COPY scrapers/ scrapers/
COPY scripts/ scripts/
COPY docker/crontab /app/crontab
COPY docker/cron-entrypoint.sh /usr/local/bin/cron-entrypoint.sh

# The schedule is installed as root's *user* crontab (`crontab <file>`), which
# has no "user" column. It deliberately does NOT live in /etc/cron.d, where
# every line would need a user name and these lines would be rejected.
RUN chmod +x /usr/local/bin/cron-entrypoint.sh \
    && chmod +x scripts/*.sh \
    && crontab /app/crontab

# Both services run from this image. The "web" service overrides CMD with
# gunicorn; the "scraper" service uses this default (cron in the foreground).
CMD ["/usr/local/bin/cron-entrypoint.sh"]
