#!/usr/bin/env python3
"""
wordle_scraper.py
==================

Fetches the NY Times Wordle answer for a given date from NYT's public
(no-login-required) endpoint and appends it to `puzzles/wordle_words.txt`
in the exact fixed-width format app.py expects:

    "YYYY-MM-DD WORD\n"   (17 bytes/record)

app.py seeks directly to `(target_date - first_date).days * 17` bytes into
the file to find a given day's word in O(1) time, so records MUST stay
perfectly contiguous (one per calendar day, no gaps, no skipped days).
This script enforces that: it backfills any gap between the file's last
recorded date and the date you're requesting.


USAGE
-----
    python wordle_scraper.py                       # today
    python wordle_scraper.py --date 2026-08-15      # a specific day
    python wordle_scraper.py --force                # re-fetch a day that is already in the file
                                                    # and overwrite its record in place
"""

import argparse
import os
import re
import sys
from datetime import date, timedelta

import requests

from scraper_common import get_with_retries

WORDLE_API = "https://www.nytimes.com/svc/wordle/v2/{date}.json"
RECORD_SIZE = 17
DEFAULT_OUTPUT = "puzzles/wordle_words.txt"
_WORD_RE = re.compile(r"^[A-Z]{5}$")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_wordle(target_date):
    url = WORDLE_API.format(date=target_date.isoformat())
    r = get_with_retries(requests, url, headers={"User-Agent": USER_AGENT}, timeout=15)
    r.raise_for_status()
    data = r.json()
    word = str(data["solution"]).strip().upper()
    # The fixed-width format can't represent anything but exactly 5 letters;
    # silently padding/truncating a surprise value would corrupt every later
    # offset, so refuse it loudly instead.
    if not _WORD_RE.match(word):
        raise ValueError(f"unexpected Wordle solution {word!r} for {target_date}")
    return word


def format_record(target_date, word):
    if not _WORD_RE.match(word):
        raise ValueError(f"refusing to write non 5-letter word {word!r}")
    record = f"{target_date.isoformat()} {word}\n"
    if len(record) != RECORD_SIZE:
        raise ValueError(f"internal error: built a {len(record)}-byte record, expected {RECORD_SIZE}")
    return record


def _read_record(f, index):
    f.seek(index * RECORD_SIZE)
    raw = f.read(RECORD_SIZE)
    if len(raw) != RECORD_SIZE:
        return None
    return raw.decode("utf-8", errors="replace")


def inspect_file(path):
    """Returns (first_date, last_date), or (None, None) for a missing/empty file.
    Raises RuntimeError if the file doesn't look like a valid fixed-width list
    (wrong size, bad dates, or a record that isn't on the date its position
    says it should be)."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None, None

    size = os.path.getsize(path)
    if size % RECORD_SIZE != 0:
        raise RuntimeError(
            f"{path} is {size} bytes, which isn't a multiple of {RECORD_SIZE}: it is truncated "
            "or isn't a fixed-width wordle file. Fix or remove it first."
        )

    count = size // RECORD_SIZE
    with open(path, "rb") as f:
        first_rec, last_rec = _read_record(f, 0), _read_record(f, count - 1)
    try:
        first = date.fromisoformat(first_rec[:10])
        last = date.fromisoformat(last_rec[:10])
    except (ValueError, TypeError):
        raise RuntimeError(f"{path} has a malformed first/last record. Fix or remove it first.")

    if (last - first).days != count - 1:
        raise RuntimeError(
            f"{path} has {count} records but spans {(last - first).days + 1} days "
            f"({first} .. {last}): there are gaps or duplicates, which would break app.py's "
            "fixed-offset lookups. Fix it first."
        )
    return first, last


def _append_one(path, d):
    word = fetch_wordle(d)
    record = format_record(d, word)
    with open(path, "ab") as f:
        f.write(record.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    print(f"  {d}: {word}")


def _overwrite_one(path, first, d):
    """Replace the record for `d` (already inside the file) with a freshly fetched one."""
    word = fetch_wordle(d)
    record = format_record(d, word)
    index = (d - first).days
    with open(path, "r+b") as f:
        old = _read_record(f, index)
        if old is None or old[:10] != d.isoformat():
            raise RuntimeError(f"record at position {index} isn't {d}; refusing to overwrite")
        f.seek(index * RECORD_SIZE)
        f.write(record.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    print(f"  {d}: {old[11:16]} -> {word}" if old[11:16] != word else f"  {d}: {word} (unchanged)")


def append_wordle(path, target_date, force=False):
    first, last = inspect_file(path)

    if first is not None and first <= target_date <= last:
        if force:
            _overwrite_one(path, first, target_date)
        else:
            print(f"  {target_date} already recorded, skipping (use --force to re-fetch it)")
        return

    if first is not None and target_date < first:
        print(f"  {target_date} is before the file's first date ({first}); can't insert "
              "into the middle of a fixed-offset file. Skipping.")
        return

    # Backfill any gap so every day in between still gets an offset.
    if last is not None:
        d = last + timedelta(days=1)
        while d < target_date:
            _append_one(path, d)
            d += timedelta(days=1)

    _append_one(path, target_date)


def main():
    parser = argparse.ArgumentParser(description="Fetch the NYT Wordle answer and append it to the app's word list.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Path to wordle_words.txt (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--force", action="store_true",
                         help="Re-fetch a date that is already in the file and overwrite its record in place")
    args = parser.parse_args()

    try:
        target = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got {args.date!r}")

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    try:
        append_wordle(args.output, target, force=args.force)
    except (requests.RequestException, ValueError, KeyError, RuntimeError, OSError) as exc:
        # A clean one-line failure for cron.log instead of a traceback. Nothing
        # that was already written is left half-done: each record is a single
        # write, and a failed day stops the backfill so the file stays contiguous.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
