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

Only "today" (and "yesterday") are available from NYT's free endpoints -
there's no official historical archive. For backfilling old dates, use
sbsolver_scraper.py instead.

USAGE
-----
    python spelling_bee_scraper.py                     # today, US/Eastern
    python spelling_bee_scraper.py --output-dir puzzles/spelling-bee
"""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

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
    """Accepts page-embedded-gameData-shaped dicts and normalizes to our internal format."""

    center = raw["centerLetter"]
    outer = raw["outerLetters"]
    answers = raw["answers"]
    print_date = raw["printDate"]

    if center is None or outer is None or answers is None:
        raise ValueError(f"missing expected fields in response: {list(raw.keys())}")

    if isinstance(outer, str):
        outer = list(outer)

    center_letter = str(center).upper()
    outer_letters = sorted(c.upper() for c in outer)
    all_letters = sorted(outer_letters + [center_letter])
    answers = sorted(a.upper() for a in answers)

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


def fetch_page_scrape(session, target_date: date) -> dict:
    resp = session.get(PAGE_URL, timeout=15)
    resp.raise_for_status()
    m = re.search(r"window\.gameData\s*=\s*(\{.*?\})\s*;?\s*</script>", resp.text, re.DOTALL)
    if not m:
        raise ValueError("couldn't find window.gameData on the Spelling Bee page")
    game_data = json.loads(m.group(1))
    raw = game_data.get("today", game_data)
    return _normalize(raw, target_date)


def output_path(output_dir: Path, date_obj) -> Path:
    return (
        output_dir
        / f"{date_obj.year:04d}"
        / f"{date_obj.month:02d}"
        / f"{date_obj.day:02d}.json"
    )


def save_puzzle(output_dir: Path, data: dict) -> Path:
    path = output_path(output_dir, data["date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": data["date"].isoformat(),
        "center_letter": data["center_letter"],
        "outer_letters": data["outer_letters"],
        "letters": data["letters"],
        "answers": data["answers"],
        "max_points": data["max_points"],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path


def main():
    parser = argparse.ArgumentParser(description="Scrape the official NYT Spelling Bee puzzle.")
    parser.add_argument("--output-dir", default="puzzles/spelling-bee",
                         help="Root folder to write year/month/day.json files into")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today, US/Eastern)")
    parser.add_argument("--overwrite", action="store_true", help="Re-fetch even if a file already exists")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else today_eastern()
    output_dir = Path(args.output_dir)

    existing = output_path(output_dir, target_date)
    if existing.exists() and not args.overwrite:
        print(f"[{target_date}] already saved -> {existing}, skipping")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})

    try:
        data = fetch_page_scrape(session, target_date)
        path = save_puzzle(output_dir, data)
        print(f"[{target_date}] saved "
              f"(center={data['center_letter']}, {len(data['answers'])} answers, "
              f"{data['max_points']} max pts) -> {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! page scrape failed: {exc}", file=sys.stderr)
        print(f"[{target_date}] FAILED - route errored:", file=sys.stderr)
        print(f"    - page scrape fallback: {exc}", file=sys.stderr)
        print("  Consider running sbsolver_scraper.py as a manual fallback for this date.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
