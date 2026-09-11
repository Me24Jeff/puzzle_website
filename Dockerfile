FROM python:3.12-slim

# cron        -> runs the scrapers on a schedule (used by the "scraper" service)
# git, rsync  -> weekly pull of the xwords archive from GitHub
# tzdata      -> so cron fires at the right wall-clock time (NYT resets on US/Eastern)
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron git rsync tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/
COPY scrapers/ scrapers/
COPY scripts/ scripts/
COPY docker/crontab /etc/cron.d/crossword-cron
COPY docker/cron-entrypoint.sh /usr/local/bin/cron-entrypoint.sh

RUN chmod 0644 /etc/cron.d/crossword-cron \
    && chmod +x /usr/local/bin/cron-entrypoint.sh \
    && chmod +x scripts/*.sh \
    && crontab /etc/cron.d/crossword-cron

# Both services run from this image. The "web" service overrides CMD with
# gunicorn; the "scraper" service uses this default (cron in the foreground).
CMD ["/usr/local/bin/cron-entrypoint.sh"]
