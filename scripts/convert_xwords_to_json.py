#!/usr/bin/env python3
"""
convert_xwords_to_json.py
=========================

One-time migration script to convert the legacy binary/gzip `puzzles/xwords`
archive (`xwords_data_NN.dat` + master index) into individual JSON files under
`puzzles/crosswords/<publisher>/<year>/<month>/<day_key>.json` (the exact same
format and directory structure used by `crossword_scraper.py` and `app.py`).

USAGE
-----
    # Inside the Docker container (working dir is /app/data):
    python3 /app/scripts/convert_xwords_to_json.py

    # Or specify paths explicitly:
    python3 scripts/convert_xwords_to_json.py \
        --xwords-dir /path/to/puzzles/xwords \
        --output-dir /path/to/puzzles/crosswords
"""

import argparse
import gzip
import json
import os
import sys
from datetime import date


def make_json_safe(obj):
    if isinstance(obj, bytes):
        return obj.decode('utf-8', errors='ignore')
    if isinstance(obj, dict):
        return {str(k): make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(item) for item in obj]
    return obj


def get_data(xwords_dir, num, start, length, mode='json', header=None):
    filename = os.path.join(xwords_dir, f"xwords_data_{num:02d}.dat")
    with open(filename, 'rb') as f:
        f.seek(start)
        data = f.read(length)

    if header is not None:
        data = header + data

    if mode == 'json':
        return json.loads(data)
    if mode == 'raw':
        return data
    if mode == 'gzip':
        return json.loads(gzip.decompress(data))
    raise ValueError(f"Invalid mode: {mode}")


def fallback_title_for(publisher, year, month, day_key):
    day_part = day_key
    kind = 'Daily'
    if day_key.endswith('-mini'):
        day_part = day_key[:-len('-mini')]
        kind = 'Mini'
    elif day_key.endswith('-midi'):
        day_part = day_key[:-len('-midi')]
        kind = 'Midi'

    if publisher == 'NY Times' and day_part.isdigit():
        try:
            dt = date(int(year), int(month), int(day_part))
            return f"The {dt.strftime('%A')} {kind}"
        except ValueError:
            pass
    return f"{publisher} - {year}-{month}-{day_key}"


def build_puzzle_json(publisher, year, month, day_key, puz_data):
    puz_data = make_json_safe(puz_data)
    width, height, cells, clues = puz_data[0], puz_data[1], puz_data[2], puz_data[3]
    meta = puz_data[6] if len(puz_data) > 6 and isinstance(puz_data[6], dict) else {}

    circled = set()
    for entry in (meta.get("flags") or []):
        if len(entry) >= 3 and "circle" in str(entry[2]).split(","):
            circled.add((entry[0], entry[1]))

    grid = []
    for y in range(height):
        row = []
        for x in range(width):
            cell = cells[y][x]
            if cell == 0:
                row.append({"block": True})
            else:
                row.append({
                    "block": False,
                    "solution": str(cell).upper() if cell else "",
                    "circled": (x, y) in circled,
                })
        grid.append(row)

    def walk(x, y, direction):
        dx, dy = (1, 0) if direction == 0 else (0, 1)
        cx, cy, spanned = x, y, []
        while 0 <= cx < width and 0 <= cy < height and cells[cy][cx] != 0:
            spanned.append([cx, cy])
            cx += dx
            cy += dy
        return spanned

    across, down = [], []
    for clue_text, direction, number, x, y, *_ in clues:
        span = walk(x, y, direction)
        entry = {
            "number": number,
            "text": clue_text,
            "row": y,
            "col": x,
            "length": len(span),
            "cells": span,
        }
        (across if direction == 0 else down).append(entry)

    across.sort(key=lambda c: c["number"])
    down.sort(key=lambda c: c["number"])

    return {
        "title": meta.get("title") or fallback_title_for(publisher, year, month, day_key),
        "author": meta.get("author", ""),
        "width": width,
        "height": height,
        "grid": grid,
        "clues": {"across": across, "down": down},
    }


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description="Convert legacy xwords .dat archive to individual JSON files.")
    parser.add_argument("--xwords-dir", default="puzzles/xwords", help="Source xwords directory (default: puzzles/xwords)")
    parser.add_argument("--output-dir", default="puzzles/crosswords", help="Target crosswords directory (default: puzzles/crosswords)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing JSON files")
    parser.add_argument("--dry-run", action="store_true", help="Count puzzles without writing files")
    args = parser.parse_args()

    index_dat = os.path.join(args.xwords_dir, "xwords_data_00.dat")
    if not os.path.isfile(index_dat):
        print(f"ERROR: {index_dat} not found.", file=sys.stderr)
        sys.exit(1)

    meta = get_data(args.xwords_dir, 0, 22, 78)
    header = get_data(args.xwords_dir, *meta[5:8], mode='raw')
    raw_index = get_data(args.xwords_dir, *meta[2:5], mode='gzip', header=header)
    master = make_json_safe(raw_index)

    converted = 0
    skipped = 0
    failed = 0

    for publisher, years in master.items():
        if not isinstance(years, dict):
            continue
        for year_str, months in years.items():
            if not isinstance(months, dict) or not str(year_str).isdigit():
                continue
            year_norm = str(int(year_str))
            for month_str, days in months.items():
                if not isinstance(days, dict) or not str(month_str).isdigit():
                    continue
                month_norm = f"{int(month_str):02d}"
                for day_key, info in days.items():
                    out_path = os.path.join(args.output_dir, publisher, year_norm, month_norm, f"{day_key}.json")
                    if os.path.exists(out_path) and not args.overwrite:
                        skipped += 1
                        continue
                    try:
                        puz_data = get_data(args.xwords_dir, *info, mode='gzip', header=header)
                        puzzle_json = build_puzzle_json(publisher, year_norm, month_norm, day_key, puz_data)
                        if not args.dry_run:
                            atomic_write_json(out_path, puzzle_json)
                        converted += 1
                        if converted % 2000 == 0:
                            print(f"  converted {converted} puzzles (skipped {skipped})...", flush=True)
                    except Exception as e:
                        failed += 1
                        print(f"  [fail] {publisher}/{year_norm}/{month_norm}/{day_key}: {e}", file=sys.stderr)

    action = "Would convert" if args.dry_run else "Converted"
    print(f"Done. {action}: {converted}, Skipped (existing): {skipped}, Failed: {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
