"""
scraper_common.py
=================

Small helpers shared by the scrapers (imported by the sibling scripts, which
all live in this folder, so `import scraper_common` just works):

  * get_with_retries()  - HTTP GET that retries timeouts, connection errors and
                          5xx / 429 responses with a growing delay, so one
                          hiccup doesn't cost a whole day's puzzle.
  * atomic_write_text() - write a file so readers (the website!) can never see
                          a half-written one, and a crash can't leave a broken
                          file behind that later runs would treat as "already
                          saved" and never fetch again.
"""

import os
import sys
import tempfile
import time

import requests

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def get_with_retries(client, url, *, retries=3, backoff=2.0, timeout=20, **kwargs):
    """GET `url` using `client` (a requests.Session or the requests module).

    Returns the final Response *without* raising for HTTP errors (callers
    decide what a 404/403 means); only transient failures are retried.
    Raises the last network exception if every attempt failed to connect.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            reason = f"{type(exc).__name__}"
        else:
            if resp.status_code not in RETRY_STATUS or attempt == retries:
                return resp
            last_exc = None
            reason = f"HTTP {resp.status_code}"
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit():
                time.sleep(min(int(retry_after), 60))
                print(f"  ! {reason} for {url}; retrying (attempt {attempt + 1}/{retries})", file=sys.stderr)
                continue

        if attempt == retries:
            break
        wait = backoff * attempt
        print(f"  ! {reason} for {url}; retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})",
              file=sys.stderr)
        time.sleep(wait)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"giving up on {url}")      # unreachable in practice


def atomic_write_text(path, text, encoding="utf-8"):
    """Write `text` to `path` atomically (temp file in the same folder, fsync,
    then os.replace)."""
    path = os.fspath(path)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)           # mkstemp creates 0600; the web container must be able to read it
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
