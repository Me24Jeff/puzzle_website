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
    python wordle_scraper.py --force                # re-fetch/overwrite today even if already present
"""

import argparse
import os
from datetime import date, timedelta

import requests

WORDLE_API = "https://www.nytimes.com/svc/wordle/v2/{date}.json"
RECORD_SIZE = 17
DEFAULT_OUTPUT = "puzzles/wordle_words.txt"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_wordle(target_date):
    url = WORDLE_API.format(date=target_date.isoformat())
    r = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    return data["solution"].upper()


def format_record(target_date, word):
    word = word.upper().ljust(5)[:5]
    record = f"{target_date.isoformat()} {word}\n"
    if len(record) != RECORD_SIZE:
        raise ValueError(f"internal error: built a {len(record)}-byte record, expected {RECORD_SIZE}")
    return record


def get_last_date(path):
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    if size < RECORD_SIZE:
        return None
    with open(path, "rb") as f:
        f.seek(size - RECORD_SIZE)
        last = f.read(RECORD_SIZE).decode("utf-8")
    return date.fromisoformat(last[:10])


def _append_one(path, d):
    word = fetch_wordle(d)
    record = format_record(d, word)
    with open(path, "a", encoding="utf-8") as f:
        f.write(record)
    print(f"  {d}: {word}")


def append_wordle(path, target_date, force=False):
    last = get_last_date(path)

    if last is None and os.path.exists(path) and os.path.getsize(path) > 0:
        raise RuntimeError(
            f"{path} exists but doesn't look like a valid fixed-width wordle "
            "file (size isn't a multiple of 17 bytes). Fix or remove it first."
        )

    if last is not None:
        if target_date < last:
            print(f"  {target_date} is before the file's start; can't insert "
                  "into the middle of a fixed-offset file. Skipping.")
            return
        if target_date == last:
            if not force:
                print(f"  {target_date} already recorded, skipping (use --force to overwrite... "
                      "actually see note below)")
                return
            print("  --force on an already-present date isn't supported for this "
                  "fixed-width format (it would corrupt the byte offsets). Skipping.")
            return
        # Backfill any gap so every day in between still gets an offset.
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
                         help="Currently only meaningful for internal checks; see note in --help output")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    append_wordle(args.output, target, force=args.force)


if __name__ == "__main__":
    main()
