#!/usr/bin/env python3
"""
connections_scraper.py
======================

Fetches OFFICIAL New York Times Connections puzzles (no login/subscription
required) from NYT's public v2 endpoint and saves one JSON file per day:

    puzzles/connections/<YYYY>/<MM>/<DD>.json

    {
        "id": 1382,
        "date": "2026-11-26",
        "editor": "Wyna Liu",
        "categories": [
            {
                "level": 0,
                "title": "TYPES OF CARS",
                "cards": [
                    {"content": "COMPACT", "position": 9},
                    ...
                ]
            },
            ...
        ]
    }

Notes on NYT's v2 schema:
  - Difficulty tiers (Yellow/Green/Blue/Purple) are encoded by the order of
    `categories` (index 0..3 -> level 0..3).
  - The curated starting 4x4 layout is encoded by each card's `position`
    (0..15, row-major). If an entry ever lacks a valid 0..15 permutation, we
    fall back to a deterministic shuffle seeded by the puzzle date.
  - Special puzzles (e.g. April Fools' 2025/2026) use `image_url` and
    `image_alt_text` instead of `content`; both are preserved.

USAGE
-----
    python connections_scraper.py                         # today (US/Eastern) + recent 3-day catch-up
    python connections_scraper.py --date 2026-11-27       # specific date
    python connections_scraper.py --backfill              # backfill from 2023-06-12 to today
    python connections_scraper.py --start-date 2026-01-01 --end-date 2026-01-31
    python connections_scraper.py --overwrite             # re-fetch existing files
"""

import argparse
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from scraper_common import atomic_write_text, get_with_retries

CONNECTIONS_API = "https://www.nytimes.com/svc/connections/v2/{date}.json"
FIRST_PUZZLE_DATE = date(2023, 6, 12)
DEFAULT_OUTPUT_DIR = "puzzles/connections"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

NYT_TZ = ZoneInfo("America/New_York")


def today_eastern() -> date:
    """NYT puzzles flip in US/Eastern, not the local server TZ."""
    return datetime.now(NYT_TZ).date()


def output_path(output_dir: Path, date_obj: date) -> Path:
    return (
        output_dir
        / f"{date_obj.year:04d}"
        / f"{date_obj.month:02d}"
        / f"{date_obj.day:02d}.json"
    )


def normalize_puzzle(raw: dict, target_date: date) -> dict:
    """Validate and normalize NYT v2 Connections JSON into our internal schema."""
    if not isinstance(raw, dict):
        raise ValueError("response is not a JSON object")

    raw_cats = raw.get("categories")
    if not isinstance(raw_cats, list) or len(raw_cats) != 4:
        raise ValueError(f"expected 4 categories, got {len(raw_cats) if isinstance(raw_cats, list) else type(raw_cats).__name__}")

    print_date = str(raw.get("print_date") or target_date.isoformat())[:10]
    try:
        date_obj = date.fromisoformat(print_date)
    except ValueError:
        date_obj = target_date

    categories = []
    positions = []

    for level, cat in enumerate(raw_cats):
        if not isinstance(cat, dict):
            raise ValueError(f"category {level} is not an object")
        title = str(cat.get("title") or "").strip()
        raw_cards = cat.get("cards")
        if not title or not isinstance(raw_cards, list) or len(raw_cards) != 4:
            raise ValueError(f"category {level} ({title!r}) does not have 4 cards")

        cards = []
        for card in raw_cards:
            if not isinstance(card, dict):
                raise ValueError(f"malformed card in category {level}")
            entry = {}
            if "content" in card and card["content"] is not None:
                text = str(card["content"]).strip()
                if not text:
                    raise ValueError(f"empty card content in category {level}")
                entry["content"] = text
            elif card.get("image_url"):
                entry["image_url"] = str(card["image_url"]).strip()
                if card.get("image_alt_text") is not None:
                    entry["image_alt_text"] = str(card["image_alt_text"]).strip()
            else:
                raise ValueError(f"card missing both content and image_url in category {level}")

            pos = card.get("position")
            if isinstance(pos, int) and 0 <= pos < 16:
                entry["position"] = pos
                positions.append(pos)
            else:
                entry["position"] = None

            cards.append(entry)

        categories.append({
            "level": level,
            "title": title,
            "cards": cards,
        })

    # Fallback: if NYT ever omits or duplicates positions, assign a deterministic
    # seeded shuffle so all players see the exact same starting grid.
    if len(positions) != 16 or set(positions) != set(range(16)):
        fallback_positions = list(range(16))
        random.Random(date_obj.isoformat()).shuffle(fallback_positions)
        idx = 0
        for cat in categories:
            for card in cat["cards"]:
                card["position"] = fallback_positions[idx]
                idx += 1

    payload = {
        "id": (date_obj - FIRST_PUZZLE_DATE).days + 1,
        "date": date_obj.isoformat(),
        "categories": categories,
    }
    if raw.get("id") is not None:
        payload["nyt_id"] = raw["id"]
    if raw.get("editor"):
        payload["editor"] = str(raw["editor"]).strip()

    return payload


def fetch_puzzle(session: requests.Session, target_date: date) -> dict | None:
    """Returns normalized dict, or None on HTTP 404 (not published)."""
    url = CONNECTIONS_API.format(date=target_date.isoformat())
    resp = get_with_retries(session, url, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return normalize_puzzle(resp.json(), target_date)


def save_puzzle(output_dir: Path, payload: dict) -> Path:
    d = date.fromisoformat(payload["date"])
    path = output_path(output_dir, d)
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def iter_dates(start_d: date, end_d: date):
    cur = start_d
    while cur <= end_d:
        yield cur
        cur += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description="Fetch NYT Connections puzzles from the public v2 API.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"Root folder for <YYYY>/<MM>/<DD>.json files (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--date", help="Single YYYY-MM-DD date to fetch")
    parser.add_argument("--start-date", help="Start YYYY-MM-DD date (inclusive)")
    parser.add_argument("--end-date", help="End YYYY-MM-DD date (inclusive)")
    parser.add_argument("--backfill", action="store_true",
                        help=f"Backfill all missing puzzles from {FIRST_PUZZLE_DATE.isoformat()} through today/end-date")
    parser.add_argument("--catchup-days", type=int, default=3,
                        help="When run with no date range, also check the previous N days for missing files (default: 3)")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Seconds to sleep between HTTP requests during multi-day runs (default: 0.2)")
    parser.add_argument("--overwrite", action="store_true", help="Re-fetch and overwrite existing files")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    today_et = today_eastern()

    try:
        if args.date:
            target = date.fromisoformat(args.date)
            dates = [target]
            # When fetching a single explicit date (e.g. tomorrow from cron),
            # also backfill any missing days in the trailing catchup window.
            for offset in range(1, max(0, args.catchup_days) + 1):
                prev = target - timedelta(days=offset)
                if prev >= FIRST_PUZZLE_DATE:
                    dates.insert(0, prev)
        elif args.backfill:
            start_d = date.fromisoformat(args.start_date) if args.start_date else FIRST_PUZZLE_DATE
            end_d = date.fromisoformat(args.end_date) if args.end_date else today_et
            if start_d < FIRST_PUZZLE_DATE:
                start_d = FIRST_PUZZLE_DATE
            if end_d < start_d:
                parser.error("--end-date is before --start-date")
            dates = list(iter_dates(start_d, end_d))
        elif args.start_date or args.end_date:
            if not (args.start_date and args.end_date):
                parser.error("--start-date and --end-date must be passed together")
            start_d = date.fromisoformat(args.start_date)
            end_d = date.fromisoformat(args.end_date)
            if end_d < start_d:
                parser.error("--end-date is before --start-date")
            dates = list(iter_dates(start_d, end_d))
        else:
            start_d = max(FIRST_PUZZLE_DATE, today_et - timedelta(days=max(0, args.catchup_days)))
            dates = list(iter_dates(start_d, today_et))
    except ValueError as exc:
        parser.error(f"Invalid date: {exc}")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    saved_count = 0
    skipped_count = 0
    missing_count = 0
    failed_count = 0
    single_target = date.fromisoformat(args.date) if args.date else None

    for idx, d in enumerate(dates):
        dest = output_path(output_dir, d)
        if dest.exists() and not args.overwrite:
            skipped_count += 1
            continue

        try:
            payload = fetch_puzzle(session, d)
        except (requests.RequestException, ValueError, KeyError) as exc:
            failed_count += 1
            print(f"[{d}] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            if single_target == d or (len(dates) == 1):
                sys.exit(1)
            continue

        if payload is None:
            missing_count += 1
            print(f"[{d}] 404 (not available)")
            if single_target == d:
                print(f"[{d}] ERROR: requested date returned 404", file=sys.stderr)
                sys.exit(1)
        else:
            path = save_puzzle(output_dir, payload)
            saved_count += 1
            pid = payload.get("id", "?")
            print(f"[{d}] saved #{pid} -> {path}")

        if idx < len(dates) - 1 and args.delay > 0:
            time.sleep(args.delay)

    print(f"Done: {saved_count} saved, {skipped_count} already on disk, {missing_count} unavailable, {failed_count} failed.")
    if failed_count > 0 and saved_count == 0 and skipped_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
