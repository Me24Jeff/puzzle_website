#!/usr/bin/env python3
"""
sbsolver_scraper.py

Scrapes sbsolver.com Spelling Bee puzzle pages (https://www.sbsolver.com/s/<index>)
and saves the letters, center letter, list of answers, and maximum total points.

Output layout:
    <output_dir>/<YYYY>/<MM>/<DD>.json

Each JSON file looks like:
    {
        "index": 3039,
        "date": "2026-09-02",
        "center_letter": "F",
        "outer_letters": ["A", "I", "L", "N", "P", "T"],
        "letters": ["A", "F", "I", "L", "N", "P", "T"],
        "answers": ["AFFIANT", "ALFALFA", "ANTIFA", ...],
        "max_points": 345
    }

Usage examples:
    # Discover the earliest/latest available puzzle indices and scrape everything
    python3 sbsolver_scraper.py --output-dir ./sbsolver_data --discover

    # Scrape a specific index range (inclusive)
    python3 sbsolver_scraper.py --output-dir ./sbsolver_data --start 1 --end 3039

    # Re-run later to pick up new puzzles + fill in anything missing (safe/resumable)
    python3 sbsolver_scraper.py --output-dir ./sbsolver_data --discover

Be polite: this script rate-limits itself (default 1 request / 0.5s) and only
fetches each puzzle once (it skips days already saved unless --overwrite is used).
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.sbsolver.com"
USER_AGENT = (
    "Mozilla/5.0 (compatible; personal-archive-bot/1.0; "
    "+https://example.com/contact) python-requests"
)

session = requests.Session()
session.headers.update({"User-AGENT": USER_AGENT})


class PuzzleNotFound(Exception):
    pass


def fetch(url, retries=3, backoff=2.0, timeout=15):
    """GET a URL with simple retry/backoff. Returns the final Response."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 404:
                raise PuzzleNotFound(url)
            resp.raise_for_status()
            return resp
        except PuzzleNotFound:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            wait = backoff * attempt
            print(f"  ! request failed ({exc}); retrying in {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
    raise last_exc


def calculate_word_points(word: str, puzzle_letters: set) -> int:
    """Calculate points for a single word based on Spelling Bee rules."""
    length = len(word)
    if length == 4:
        points = 1
    elif length > 4:
        points = length
    else:
        points = 1  # Fallback for any unexpected short words

    # Pangram check: uses all 7 unique puzzle letters
    if set(word.upper()) == puzzle_letters:
        points += 7

    return points


def parse_puzzle(html):
    """Extract letters, center letter, answers, and max points from a puzzle page's HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # --- Date, from the <title> tag
    title = soup.title.string if soup.title else ""
    date_match = re.match(r"\s*([A-Za-z]+ \d{1,2}, \d{4})", title or "")
    if not date_match:
        raise ValueError(f"could not find date in title: {title!r}")
    date_obj = datetime.strptime(date_match.group(1), "%B %d, %Y").date()

    # --- Letters, from the input field
    string_input = soup.find("input", {"id": "string"})
    if string_input is None or not string_input.get("value"):
        raise ValueError("could not find letter string input (#string)")
    letter_string = string_input["value"].strip()
    if len(letter_string) != 7:
        raise ValueError(f"unexpected letter string {letter_string!r} (want 7 chars)")

    center_candidates = [c for c in letter_string if c.isupper()]
    if len(center_candidates) != 1:
        raise ValueError(f"could not determine unique center letter from {letter_string!r}")
    center_letter = center_candidates[0].upper()
    outer_letters = sorted(c.upper() for c in letter_string if c.islower())
    all_letters = sorted(outer_letters + [center_letter])
    puzzle_letter_set = set(all_letters)

    # --- Answers, from the solution table
    answer_table = soup.find("table", class_="bee-set")
    if answer_table is None:
        raise ValueError("could not find answers table (table.bee-set)")

    answers = []
    for a in answer_table.find_all("a", href=True):
        m = re.search(r"/h/([A-Za-z]+)$", a["href"])
        if m:
            answers.append(m.group(1).upper())

    if not answers:
        raise ValueError("no answers parsed from answers table")

    # --- Calculate maximum total points
    max_points = sum(calculate_word_points(word, puzzle_letter_set) for word in answers)

    return {
        "date": date_obj,
        "center_letter": center_letter,
        "outer_letters": outer_letters,
        "letters": all_letters,
        "answers": answers,
        "max_points": max_points,
    }


def output_path(output_dir: Path, date_obj) -> Path:
    return (
        output_dir
        / f"{date_obj.year:04d}"
        / f"{date_obj.month:02d}"
        / f"{date_obj.day:02d}.json"
    )


def save_puzzle(output_dir: Path, index: int, data: dict):
    path = output_path(output_dir, data["date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "index": index,
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


def discover_bounds():
    """Follow /earliest and /latest to find the valid index range."""
    earliest_resp = fetch(f"{BASE}/earliest")
    latest_resp = fetch(f"{BASE}/latest")

    def index_from_url(url):
        m = re.search(r"/s?/?(\d+)/?$", url)
        if not m:
            m = re.search(r"/(\d+)$", url)
        if not m:
            raise ValueError(f"could not extract index from redirected URL: {url}")
        return int(m.group(1))

    return index_from_url(earliest_resp.url), index_from_url(latest_resp.url)


def scrape_index(output_dir: Path, index: int, overwrite: bool, delay: float,
                  index_cache: set):
    if not overwrite and index in index_cache:
        print(f"[{index}] already saved, skipping")
        return True

    url = f"{BASE}/s/{index}"
    try:
        resp = fetch(url)
    except PuzzleNotFound:
        print(f"[{index}] 404 not found, skipping")
        return False

    try:
        data = parse_puzzle(resp.text)
    except ValueError as exc:
        print(f"[{index}] ! failed to parse ({exc})", file=sys.stderr)
        return False

    path = save_puzzle(output_dir, index, data)
    print(f"[{index}] saved {data['date'].isoformat()} (Max Pts: {data['max_points']}) -> {path}")
    time.sleep(delay)
    return True


def load_index_cache(output_dir: Path) -> set:
    """Build a set of indices already saved, so re-runs can skip them fast."""
    cache = set()
    for p in output_dir.glob("*/*/*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                idx = json.load(f).get("index")
                if idx is not None:
                    cache.add(idx)
        except (OSError, json.JSONDecodeError):
            continue
    return cache


def main():
    parser = argparse.ArgumentParser(description="Scrape sbsolver.com Spelling Bee archives.")
    parser.add_argument("--output-dir", required=True, help="Root folder to write year/month/day files into")
    parser.add_argument("--start", type=int, help="Start index (inclusive)")
    parser.add_argument("--end", type=int, help="End index (inclusive)")
    parser.add_argument("--discover", action="store_true",
                         help="Auto-discover earliest/latest indices via /earliest and /latest")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds to sleep between requests (default 0.5)")
    parser.add_argument("--overwrite", action="store_true", help="Re-fetch and overwrite already-saved days")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.discover:
        print("Discovering earliest/latest available puzzle indices...")
        start, end = discover_bounds()
        print(f"Found range: {start} .. {end}")
    else:
        if args.start is None or args.end is None:
            parser.error("either --discover, or both --start and --end, are required")
        start, end = args.start, args.end

    index_cache = set() if args.overwrite else load_index_cache(output_dir)

    ok, failed = 0, 0
    for index in range(start, end + 1):
        try:
            if scrape_index(output_dir, index, args.overwrite, args.delay, index_cache):
                ok += 1
            else:
                failed += 1
        except KeyboardInterrupt:
            print("\nInterrupted by user. Progress so far is saved; re-run to resume.")
            sys.exit(1)

    print(f"\nDone. {ok} succeeded, {failed} failed/skipped, range {start}-{end}.")


if __name__ == "__main__":
    main()
