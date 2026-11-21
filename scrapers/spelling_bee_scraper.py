#!/usr/bin/env python3
"""
spelling_bee_scraper.py
========================

Fetches the OFFICIAL New York Times Spelling Bee puzzle (no login/subscription
required - Spelling Bee, unlike the Crossword, is free) and saves it in the
exact JSON shape app.py / spelling-bee.html expect:

    puzzles/spelling-bee/<YYYY>/<MM>/<DD>.json

    {
        "date": "2026-09-10",
        "center_letter": "I",
        "outer_letters": ["A", "C", "E", "F", "L", "T"],
        "letters": ["A", "C", "E", "F", "I", "L", "T"],
        "answers": ["ACETIC", "FACILE", ...],
        "max_points": 303
    }

NYT's page only carries the current puzzle plus the previous one(s) - there's
no official historical archive. Each run saves every puzzle on the page that
isn't on disk yet, so if a day's run ever fails (network blip, NYT outage) the
next successful run fills the gap automatically while it's still on the page.
For older dates, use sbsolver_scraper.py instead.

USAGE
-----
    python spelling_bee_scraper.py                     # today (US/Eastern) + any recent missing days
    python spelling_bee_scraper.py --date 2026-09-09   # one specific day (must still be on NYT's page)
    python spelling_bee_scraper.py --overwrite         # re-save puzzles even if a file exists
    python spelling_bee_scraper.py --output-dir puzzles/spelling-bee
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from scraper_common import atomic_write_text, get_with_retries

PAGE_URL = "https://www.nytimes.com/puzzles/spelling-bee"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

NYT_TZ = ZoneInfo("America/New_York")


def today_eastern() -> date:
    """NYT puzzles flip at midnight/3am US/Eastern, not the local server TZ."""
    return datetime.now(NYT_TZ).date()


def calculate_word_points(word: str, puzzle_letters: set) -> int:
    length = len(word)
    points = 1 if length == 4 else (length if length > 4 else 1)
    if set(word.upper()) == puzzle_letters:
        points += 7
    return points


def _normalize(raw: dict, fallback_date: date) -> dict:
    """Accepts page-embedded-gameData-shaped dicts and normalizes to our internal format.
    Raises ValueError if anything essential is missing or implausible, so a
    layout change on NYT's side fails loudly instead of saving a broken puzzle."""

    center = raw.get("centerLetter")
    outer = raw.get("outerLetters")
    answers = raw.get("answers")
    print_date = raw.get("printDate")

    if center is None or outer is None or not answers:
        raise ValueError(f"missing expected fields in puzzle data: {sorted(raw.keys())}")

    if isinstance(outer, str):
        outer = list(outer)

    center_letter = str(center).upper()
    outer_letters = sorted(str(c).upper() for c in outer)
    all_letters = sorted(set(outer_letters) | {center_letter})
    answers = sorted({str(a).upper() for a in answers})      # de-duplicated

    if len(center_letter) != 1 or not center_letter.isalpha() or len(all_letters) != 7 or len(outer_letters) != 6:
        raise ValueError(f"implausible letters: center={center_letter!r} outer={outer_letters!r}")

    if print_date:
        try:
            date_obj = datetime.strptime(str(print_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            date_obj = fallback_date
    else:
        date_obj = fallback_date

    puzzle_letter_set = set(all_letters)
    max_points = sum(calculate_word_points(w, puzzle_letter_set) for w in answers)

    return {
        "date": date_obj,
        "center_letter": center_letter,
        "outer_letters": outer_letters,
        "letters": all_letters,
        "answers": answers,
        "max_points": max_points,
    }


def _collect_puzzles(node, found, depth=0):
    """Recursively find every puzzle-shaped dict (has centerLetter + answers)."""
    if depth > 5:
        return
    if isinstance(node, dict):
        if "centerLetter" in node and "answers" in node:
            found.append(node)
            return
        for v in node.values():
            _collect_puzzles(v, found, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _collect_puzzles(v, found, depth + 1)


def parse_page(html: str, target_date: date) -> dict:
    """{date: normalized puzzle} for every puzzle embedded in the page's
    window.gameData (today's, plus yesterday's etc. when NYT includes them)."""
    m = re.search(r"window\.gameData\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.DOTALL)
    if not m:
        raise ValueError("couldn't find window.gameData on the Spelling Bee page")
    game_data = json.loads(m.group(1))

    raws = []
    _collect_puzzles(game_data, raws)
    if not raws:
        raise ValueError(f"no puzzle found inside window.gameData (keys: {sorted(game_data.keys())})")

    puzzles = {}
    for raw in raws:
        # Only used when the entry carries no printDate of its own.
        try:
            data = _normalize(raw, target_date)
        except ValueError as exc:
            print(f"  ! skipping one malformed puzzle on the page: {exc}", file=sys.stderr)
            continue
        puzzles[data["date"]] = data
    if not puzzles:
        raise ValueError("every puzzle on the page was malformed")
    return puzzles


def fetch_page_scrape(session, target_date: date) -> dict:
    resp = get_with_retries(session, PAGE_URL, timeout=20)
    resp.raise_for_status()
    return parse_page(resp.text, target_date)


def output_path(output_dir: Path, date_obj) -> Path:
    return (
        output_dir
        / f"{date_obj.year:04d}"
        / f"{date_obj.month:02d}"
        / f"{date_obj.day:02d}.json"
    )


def save_puzzle(output_dir: Path, data: dict) -> Path:
    path = output_path(output_dir, data["date"])
    payload = {
        "date": data["date"].isoformat(),
        "center_letter": data["center_letter"],
        "outer_letters": data["outer_letters"],
        "letters": data["letters"],
        "answers": data["answers"],
        "max_points": data["max_points"],
    }
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def main():
    parser = argparse.ArgumentParser(description="Scrape the official NYT Spelling Bee puzzle.")
    parser.add_argument("--output-dir", default="puzzles/spelling-bee",
                         help="Root folder to write year/month/day.json files into")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today, US/Eastern)")
    parser.add_argument("--overwrite", action="store_true", help="Re-fetch even if a file already exists")
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date) if args.date else today_eastern()
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got {args.date!r}")
    output_dir = Path(args.output_dir)

    existing = output_path(output_dir, target_date)
    if existing.exists() and not args.overwrite:
        print(f"[{target_date}] already saved -> {existing}, skipping")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})

    try:
        puzzles = fetch_page_scrape(session, target_date)
    except Exception as exc:  # noqa: BLE001
        print(f"[{target_date}] FAILED - page scrape: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  Consider running sbsolver_scraper.py as a manual fallback for this date.",
              file=sys.stderr)
        sys.exit(1)

    # What to save: a specific --date saves just that day; the default run saves
    # everything on the page that is missing (today + any recent gap).
    if args.date:
        wanted = [puzzles[target_date]] if target_date in puzzles else []
        if not wanted:
            have = ", ".join(sorted(d.isoformat() for d in puzzles))
            print(f"[{target_date}] FAILED - NYT's page only has: {have}", file=sys.stderr)
            print("  Consider running sbsolver_scraper.py as a manual fallback for this date.",
                  file=sys.stderr)
            sys.exit(1)
    else:
        wanted = [puzzles[d] for d in sorted(puzzles)]

    saved_any = False
    for data in wanted:
        if output_path(output_dir, data["date"]).exists() and not args.overwrite:
            continue
        path = save_puzzle(output_dir, data)
        saved_any = True
        print(f"[{data['date']}] saved "
              f"(center={data['center_letter']}, {len(data['answers'])} answers, "
              f"{data['max_points']} max pts) -> {path}")

    if not saved_any:
        print(f"[{target_date}] nothing new on the page (have: "
              f"{', '.join(sorted(d.isoformat() for d in puzzles))})")
    if not args.date and target_date not in puzzles:
        # NYT's page doesn't have *today's* puzzle (yet): flag it with a
        # non-zero exit so it shows up as a failure rather than a quiet success.
        print(f"[{target_date}] WARNING - page has no puzzle for this date yet", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
