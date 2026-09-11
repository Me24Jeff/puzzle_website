#!/usr/bin/env python3
"""
crossword_scraper.py
=====================

Fetches NY Times crosswords (Daily, Mini, and Midi) using the same approach
as https://github.com/Q726kbXuN/nytxw_puz's nyt.py, and writes them out as
JSON files in the format this app's `build_puzzle_json()` already produces
(title/author/width/height/grid/clues), instead of an Across Lite .puz file.

WHY THIS EXISTS
---------------
The app's main puzzle archive (`puzzles/xwords/xwords_data_NN.dat` + the
master index) is a pre-baked historical bundle. There's no clean way to
append a single new day's puzzle into that binary/gzip format. Instead,
this script drops each freshly-scraped puzzle as its own small JSON file
into `puzzles/crosswords/<publisher>/<year>/<month>/<day_key>.json`, and
`app.py` has been updated to fall back to that folder any time a puzzle
isn't found in the master archive. So new puzzles "just work" in the
website without touching the historical bundle at all.

AUTHENTICATION
---------------
The Daily/Mini/Midi crosswords require an active NY Times Games
subscription. The original nyt.py grabs cookies straight out of your
browser (via browser_cookie3), which doesn't make sense in a headless
Docker container. Instead, this script expects your `NYT-S` session
cookie to be supplied directly:

  1. --cookie "<value>"                 (explicit, lowest priority to change per-run)
  2. $NYT_S_COOKIE environment variable (recommended for Docker)
  3. a cookie file (JSON: {"NYT-S": "..."}, or a bare token), see --cookie-file

To get your NYT-S cookie: log into nytimes.com in a normal browser with
your subscription, open DevTools -> Application/Storage -> Cookies ->
https://www.nytimes.com, and copy the value of the cookie named `NYT-S`.
Treat it like a password - anyone with it can read puzzles as you.

USAGE
-----
    # Today's Daily, Mini, and Midi:
    python crossword_scraper.py --type all

    # Just today's Daily:
    python crossword_scraper.py --type daily

    # A specific date:
    python crossword_scraper.py --type mini --date 2026-08-15

    # Backfill a range:
    python crossword_scraper.py --type daily --start-date 2026-08-01 --end-date 2026-08-31

LIMITATIONS
-----------
- If NY Times changes their page/API structure this will need updating,
  same as the upstream nytxw_puz project.
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests

# --------------------------------------------------------------------------
# Config matching app.py's TYPE_FOLDER_CONFIG
# --------------------------------------------------------------------------
PUBLISHER = "NY Times"

TYPE_CONFIG = {
    "daily": {"url_path": "daily", "suffix": ""},
    "mini":  {"url_path": "mini",  "suffix": "-mini"},
    "midi":  {"url_path": "midi",  "suffix": "-midi"},
}

DEFAULT_OUTPUT_DIR = "puzzles/crosswords"

NYT_TYPE_BLOCK = 0
NYT_TYPE_CIRCLED = 2
NYT_TYPE_GRAY = 3

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------
# Auth / session
# --------------------------------------------------------------------------
def load_cookie(cli_cookie, cookie_file):
    if cli_cookie:
        return cli_cookie

    env_cookie = os.environ.get("NYT_S_COOKIE")
    if env_cookie:
        return env_cookie

    if cookie_file and os.path.exists(cookie_file):
        with open(cookie_file, "r", encoding="utf-8") as f:
            contents = f.read().strip()
        if contents.startswith("{"):
            data = json.loads(contents)
            for key in ("NYT-S", "nyt-s", "NYT_S"):
                if key in data:
                    return data[key]
            raise RuntimeError(f"{cookie_file} doesn't contain an 'NYT-S' key")
        return contents  # treat the whole file as the raw token

    raise RuntimeError(
        "No NYT-S cookie found. Pass --cookie, set NYT_S_COOKIE, or provide "
        f"--cookie-file (default lookup: {cookie_file})."
    )


def build_session(nyt_s_cookie):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.cookies.set("NYT-S", nyt_s_cookie, domain=".nytimes.com")
    return session


# --------------------------------------------------------------------------
# Fetching, mirrors nyt.py's get_puzzle() / get_puzzle_from_id()
# --------------------------------------------------------------------------
def get_puzzle_from_id(session, puzzle_id):
    puzzle_url = f"https://www.nytimes.com/svc/crosswords/v6/puzzle/{puzzle_id}.json"
    r = session.get(puzzle_url, timeout=20)
    r.raise_for_status()
    new_format = r.json()

    resp = new_format["body"][0]
    resp["meta"] = {}
    for cur in ["publicationDate", "title", "editor", "copyright", "constructors", "notes"]:
        if cur in new_format:
            resp["meta"][cur] = new_format[cur]

    resp["dimensions"]["columnCount"] = resp["dimensions"]["width"]
    resp["dimensions"]["rowCount"] = resp["dimensions"]["height"]
    return resp


def get_puzzle(session, url):
    r = session.get(url, timeout=20)
    r.raise_for_status()
    page = r.text

    # Current NYT puzzle pages embed `window.gameData = {...}` with a
    # "filename" key pointing at the puzzle's canonical id.
    m = re.search(r"window\.gameData\s*=\s*(?P<json>\{.*?\})\s*;?\s*</script>", page, re.DOTALL)
    if not m:
        raise RuntimeError(
            "Couldn't find puzzle data on the page. Either the URL has no "
            "puzzle for that date, your NYT-S cookie is invalid/expired, or "
            "NY Times changed their page layout."
        )

    key_data = json.loads(m.group("json"))
    filename = key_data.get("filename")
    if not filename:
        raise RuntimeError("Found window.gameData but no 'filename' field in it.")

    meta_url = f"https://www.nytimes.com/svc/crosswords/v6/puzzle/{filename}.json"
    r2 = session.get(meta_url, timeout=20)
    r2.raise_for_status()
    metadata = r2.json()
    if "id" not in metadata:
        raise RuntimeError(f"Unexpected response from {meta_url}: missing 'id'")

    return get_puzzle_from_id(session, metadata["id"])


# --------------------------------------------------------------------------
# Conversion: raw NYT puzzle JSON -> this app's puzzle JSON format
# (Mirrors build_puzzle_json() in app.py, so play.html doesn't need to
# know whether a puzzle came from the master archive or the scraper.)
# --------------------------------------------------------------------------
def _clue_text(clues_lookup, idx):
    entry = clues_lookup[idx]
    t = entry.get("text", "")
    if isinstance(t, list):
        t = t[0] if t else ""
    if isinstance(t, dict):
        t = t.get("plain", "")
    return html.unescape(t or "")


def convert_to_app_json(raw, fallback_title):
    width = raw["dimensions"]["columnCount"]
    height = raw["dimensions"]["rowCount"]
    cells = raw["cells"]
    meta = raw.get("meta", {})
    clues_lookup = raw.get("clues", [])

    def cell_at(x, y):
        return cells[y * width + x]

    def is_block(c):
        if "answer" not in c:
            return True
        if c.get("type") == NYT_TYPE_BLOCK:
            return True
        return False

    grid = []
    for y in range(height):
        row = []
        for x in range(width):
            c = cell_at(x, y)
            if is_block(c):
                row.append({"block": True})
            else:
                answer = c.get("answer", "")
                letter = answer.upper() if answer else ""
                circled = c.get("type") in (NYT_TYPE_CIRCLED, NYT_TYPE_GRAY)
                row.append({"block": False, "solution": letter, "circled": circled})
        grid.append(row)

    def walk(x, y, direction):
        dx, dy = (1, 0) if direction == 0 else (0, 1)
        cx, cy, spanned = x, y, []
        while 0 <= cx < width and 0 <= cy < height and not grid[cy][cx]["block"]:
            spanned.append([cx, cy])
            cx += dx
            cy += dy
        return spanned

    # 1. Collect all unique clue IDs in reading order
    seen_ids = set()
    ordered_clue_ids = []
    for c in cells:
        for clue_id in c.get("clues", []):
            if clue_id not in seen_ids:
                seen_ids.add(clue_id)
                ordered_clue_ids.append(clue_id)

    # 2. Consume clue IDs sequentially as numbered cells are encountered
    across, down = [], []
    clue_iter = iter(ordered_clue_ids)
    number = 0

    for y in range(height):
        for x in range(width):
            if grid[y][x]["block"]:
                continue
            starts_across = (x == 0 or grid[y][x - 1]["block"]) and \
                             (x + 1 < width and not grid[y][x + 1]["block"])
            starts_down = (y == 0 or grid[y - 1][x]["block"]) and \
                          (y + 1 < height and not grid[y + 1][x]["block"])
            if not (starts_across or starts_down):
                continue

            number += 1

            if starts_across:
                idx = next(clue_iter, None)
                if idx is not None:
                    span = walk(x, y, 0)
                    across.append({
                        "number": number, "text": _clue_text(clues_lookup, idx),
                        "row": y, "col": x, "length": len(span), "cells": span,
                    })
            if starts_down:
                idx = next(clue_iter, None)
                if idx is not None:
                    span = walk(x, y, 1)
                    down.append({
                        "number": number, "text": _clue_text(clues_lookup, idx),
                        "row": y, "col": x, "length": len(span), "cells": span,
                    })

    across.sort(key=lambda c: c["number"])
    down.sort(key=lambda c: c["number"])

    constructors = meta.get("constructors", [])
    author = ", ".join(constructors) if constructors else ""
    if meta.get("editor"):
        author = f"{author} / {meta['editor']}" if author else meta["editor"]

    return {
        "title": meta.get("title") or fallback_title,
        "author": author,
        "width": width,
        "height": height,
        "grid": grid,
        "clues": {"across": across, "down": down},
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def output_path(output_dir, puzzle_type, target_date):
    cfg = TYPE_CONFIG[puzzle_type]
    y, m, d = target_date.year, target_date.month, target_date.day
    return os.path.join(
        output_dir, PUBLISHER, str(y), f"{m:02d}", f"{d:02d}{cfg['suffix']}.json"
    )


def scrape_one(session, puzzle_type, target_date, output_dir, force=False):
    cfg = TYPE_CONFIG[puzzle_type]
    out_path = output_path(output_dir, puzzle_type, target_date)

    if os.path.exists(out_path) and not force:
        print(f"  [skip] {puzzle_type:5s} {target_date} - already saved")
        return True

    y, m, d = target_date.year, target_date.month, target_date.day
    url = f"https://www.nytimes.com/crosswords/game/{cfg['url_path']}/{y}/{m:02d}/{d:02d}"

    try:
        raw = get_puzzle(session, url)
        fallback_title = f"NY Times {puzzle_type.title()} - {target_date.isoformat()}"
        data = convert_to_app_json(raw, fallback_title)
    except Exception as e:
        print(f"  [fail] {puzzle_type:5s} {target_date} - {e}")
        return False

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"  [ok]   {puzzle_type:5s} {target_date} -> {out_path}")
    return True


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description="Scrape NY Times crosswords into this app's puzzle JSON format.")
    parser.add_argument("--type", choices=["daily", "mini", "midi", "all"], default="all",
                         help="Which puzzle type(s) to fetch (default: all)")
    parser.add_argument("--date", help="Single date to fetch, YYYY-MM-DD (default: today)")
    parser.add_argument("--start-date", help="Start of a date range to backfill, YYYY-MM-DD")
    parser.add_argument("--end-date", help="End of a date range to backfill, YYYY-MM-DD (default: today)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                         help=f"Where to write puzzle JSON files (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--cookie", help="Your NYT-S cookie value (overrides env/file)")
    parser.add_argument("--cookie-file", default="nyt_cookies.json",
                         help="File containing your NYT-S cookie (JSON {'NYT-S': ...} or raw token)")
    parser.add_argument("--force", action="store_true", help="Re-download even if a file already exists")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to sleep between requests (default: 1.0)")
    args = parser.parse_args()

    types = list(TYPE_CONFIG.keys()) if args.type == "all" else [args.type]

    if args.start_date:
        start = date.fromisoformat(args.start_date)
        end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    else:
        single = date.fromisoformat(args.date) if args.date else date.today()
        start = end = single

    try:
        cookie = load_cookie(args.cookie, args.cookie_file)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    session = build_session(cookie)

    ok_count, fail_count = 0, 0
    first = True
    for target_date in daterange(start, end):
        for puzzle_type in types:
            if not first:
                time.sleep(args.delay)
            first = False
            if scrape_one(session, puzzle_type, target_date, args.output_dir, force=args.force):
                ok_count += 1
            else:
                fail_count += 1

    print(f"\nDone. {ok_count} succeeded, {fail_count} failed.")
    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
