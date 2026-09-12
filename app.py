from flask import Flask, render_template, jsonify, request, session, redirect, Response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import os, gzip, json, sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'ece0f05ebddc786379eb4db9cd8c868ae2c310970b602bf6d436a4a3f97833bc')

PUZZLE_DIR = "puzzles/xwords"
DB_PATH = "puzzle_progress.db"

# Fallback folder for individually-scraped crosswords (see crossword_scraper.py).
# PUZZLE_DIR/the master index is a pre-baked historical archive that isn't meant
# to be appended to day-by-day. Instead, freshly-scraped puzzles are dropped in
# here as one small JSON file per puzzle, already in the exact shape
# build_puzzle_json() below produces. Any time a puzzle can't be found in the
# master archive, we check here before giving up.
CROSSWORD_FALLBACK_DIR = "puzzles/crosswords"

# Plain-text word list for Wordle. One word per line; the LAST line is
# always today's word, and each line above it is the day before, so the
# file is simply appended to once a day. Nothing in this app writes to
# this file automatically - it's expected to be maintained externally.
WORDLE_WORDS_FILE = "puzzles/wordle_words.txt"
WORDLE_MAX_GUESSES = 6

# Plain-text dictionary of every word players are allowed to guess (not just
# possible answers). One word per line, alphabetically sorted. Served to the
# client so it can validate guesses before accepting them.
WORDLE_VALID_WORDS_FILE = "puzzles/valid_wordle_words.txt"

# One JSON file per day, same idea as the Wordle word list but Spelling Bee
# needs more than a single word so each day gets its own file instead of a
# packed record. Layout: puzzles/spelling-bee/<year>/<month>/<day>.json
SPELLING_BEE_DIR = "puzzles/spelling-bee"

if not os.path.exists(PUZZLE_DIR):
    os.makedirs(PUZZLE_DIR)

if not os.path.exists(CROSSWORD_FALLBACK_DIR):
    os.makedirs(CROSSWORD_FALLBACK_DIR)

if not os.path.exists(SPELLING_BEE_DIR):
    os.makedirs(SPELLING_BEE_DIR)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                avatar TEXT DEFAULT '🧩'
            )
        ''')

        # Column migration for avatar if upgrading existing DB
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'avatar' not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT '🧩'")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS progress (
                user_id INTEGER,
                publisher TEXT,
                year TEXT,
                month TEXT,
                day_key TEXT,
                user_grid TEXT,
                timer_seconds INTEGER,
                ratio REAL,
                completed INTEGER,
                used_check INTEGER,
                PRIMARY KEY (user_id, publisher, year, month, day_key)
            )
        ''')
        conn.commit()

init_db()

MASTER_INDEX = None

def make_json_safe(obj):
    if isinstance(obj, bytes):
        return obj.decode('utf-8', errors='ignore')
    if isinstance(obj, dict):
        return {str(k): make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(item) for item in obj]
    return obj

def get_data(num, start, length, mode='json', header=None):
    filename = os.path.join(PUZZLE_DIR, f"xwords_data_{num:02d}.dat")
    if os.path.exists(filename):
        with open(filename, 'rb') as f:
            full_chunk = f.read()
    else:
        raise FileNotFoundError(f"Puzzle data file {filename} not found locally.")

    data = full_chunk[start:start+length]
    if header is not None:
        data = header + data

    if mode == 'json':
        return json.loads(data)
    elif mode == 'raw':
        return data
    elif mode == 'gzip':
        decompressed = gzip.decompress(data)
        return json.loads(decompressed)
    else:
        raise Exception("Invalid mode")

def get_master_index():
    global MASTER_INDEX
    if MASTER_INDEX is None:
        meta = get_data(0, 22, 78)
        header = get_data(*meta[5:8], mode='raw')
        raw_index = get_data(*meta[2:5], mode='gzip', header=header)
        MASTER_INDEX = make_json_safe(raw_index)
    return MASTER_INDEX

def build_puzzle_json(name, puz_data):
    width, height, cells, clues = puz_data[0], puz_data[1], puz_data[2], puz_data[3]
    meta = puz_data[6] if len(puz_data) > 6 else {}

    circled = set()
    for x, y, flags in meta.get("flags", []):
        if "circle" in flags.split(","):
            circled.add((x, y))

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
                    "solution": cell.upper() if cell else "",
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
            "number": number, "text": clue_text,
            "row": y, "col": x, "length": len(span), "cells": span,
        }
        (across if direction == 0 else down).append(entry)

    across.sort(key=lambda c: c["number"])
    down.sort(key=lambda c: c["number"])

    return {
        "title": meta.get("title") or name,
        "author": meta.get("author", ""),
        "width": width,
        "height": height,
        "grid": grid,
        "clues": {"across": across, "down": down},
    }

def get_puzzle_title(puz_data):
    meta = puz_data[6] if len(puz_data) > 6 else {}
    return meta.get("title") or "Crack the clues in today's puzzle."

# --- Fallback folder helpers (scraped puzzles not in the master archive) ---

def fallback_puzzle_path(publisher, year, month, day_key):
    return os.path.join(
        CROSSWORD_FALLBACK_DIR, publisher, str(int(year)), f"{int(month):02d}", f"{day_key}.json"
    )

def load_fallback_puzzle_json(publisher, year, month, day_key):
    """Returns the pre-built puzzle JSON for a scraped puzzle, or None if
    there isn't one on disk for this publisher/year/month/day_key."""
    path = fallback_puzzle_path(publisher, year, month, day_key)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def get_fallback_puzzle_days(publisher, suffix, year, month_padded):
    """Days (as ints) in the scraped-puzzle fallback folder for a given
    publisher/type/year/month, mirroring get_puzzle_days_in_month() below
    but scanning puzzles/crosswords instead of the master index."""
    folder = os.path.join(CROSSWORD_FALLBACK_DIR, publisher, str(int(year)), month_padded)
    if not os.path.isdir(folder):
        return []

    days = []
    for fname in os.listdir(folder):
        if not fname.endswith('.json'):
            continue
        day_key = fname[:-len('.json')]
        if suffix:
            if not day_key.endswith(suffix):
                continue
            day_part = day_key[:-len(suffix)]
        else:
            # The 'normal' (no-suffix) type shouldn't swallow "-mini"/"-midi"
            # files that just happen to also end in digits before the dash.
            if '-' in day_key:
                continue
            day_part = day_key
        if day_part.isdigit():
            days.append(int(day_part))
    return days

# Wordle word-list helpers
RECORD_SIZE = 17  # "YYYY-MM-DD WORD\n" (10 + 1 + 5 + 1 = 17 bytes)

def get_wordle_word_for_date(target_date: date) -> str | None:
    """Seeks directly to target date offset in O(1) time."""
    if not os.path.exists(WORDLE_WORDS_FILE):
        return None

    file_size = os.path.getsize(WORDLE_WORDS_FILE)
    if file_size < RECORD_SIZE:
        return None

    with open(WORDLE_WORDS_FILE, "rb") as f:
        # Read first record to determine start date
        first_record = f.read(RECORD_SIZE).decode("utf-8")
        try:
            start_date = date.fromisoformat(first_record[:10])
        except ValueError:
            return None

        idx = (target_date - start_date).days
        byte_offset = idx * RECORD_SIZE

        # Bounds check against file size
        if byte_offset < 0 or byte_offset + RECORD_SIZE > file_size:
            return None

        # Seek straight to target line
        f.seek(byte_offset)
        record = f.read(RECORD_SIZE).decode("utf-8").strip()
        parts = record.split()

        if len(parts) >= 2 and parts[0] == target_date.isoformat():
            return parts[1].upper()

    return None


def get_wordle_available_range():
    """Reads only the first and last 17 bytes of the file."""
    if not os.path.exists(WORDLE_WORDS_FILE):
        return None, None

    file_size = os.path.getsize(WORDLE_WORDS_FILE)
    if file_size < RECORD_SIZE:
        return None, None

    with open(WORDLE_WORDS_FILE, "rb") as f:
        first_record = f.read(RECORD_SIZE).decode("utf-8")

        # Seek to final record
        f.seek(file_size - RECORD_SIZE)
        last_record = f.read(RECORD_SIZE).decode("utf-8")

        try:
            start_date = date.fromisoformat(first_record[:10])
            end_date = date.fromisoformat(last_record[:10])
            return start_date, end_date
        except ValueError:
            return None, None

_VALID_WORDLE_WORDS_CACHE = None

def get_valid_wordle_words_text():
    """Reads the newline-delimited valid-guess dictionary and caches it in
    memory so we don't hit disk on every request."""
    global _VALID_WORDLE_WORDS_CACHE
    if _VALID_WORDLE_WORDS_CACHE is None:
        if os.path.exists(WORDLE_VALID_WORDS_FILE):
            with open(WORDLE_VALID_WORDS_FILE, "r", encoding="utf-8") as f:
                _VALID_WORDLE_WORDS_CACHE = f.read()
        else:
            _VALID_WORDLE_WORDS_CACHE = ""
    return _VALID_WORDLE_WORDS_CACHE

# Auth Routes
@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400

    hashed = generate_password_hash(password)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # Check for existing username case-insensitively
            cursor.execute('SELECT id FROM users WHERE LOWER(username) = LOWER(?)', (username,))
            if cursor.fetchone():
                return jsonify({'error': 'Username already taken'}), 400

            cursor.execute('INSERT INTO users (username, password_hash, avatar) VALUES (?, ?, ?)', (username, hashed, '🧩'))
            conn.commit()
            user_id = cursor.lastrowid
            session['user_id'] = user_id
            session['username'] = username
            session['avatar'] = '🧩'
            return jsonify({'status': 'success'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Username already taken'}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Case-insensitive query for username
        cursor.execute('SELECT id, username, password_hash, avatar FROM users WHERE LOWER(username) = LOWER(?)', (username,))
        user = cursor.fetchone()

        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['username'] = user[1]  # Retains stored case preference
            session['avatar'] = user[3] or '🧩'
            return jsonify({'status': 'success'})
        return jsonify({'error': 'Invalid username or password'}), 401

@app.route('/api/change-username', methods=['POST'])
def change_username():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    new_username = data.get('username', '').strip()

    if not new_username:
        return jsonify({'error': 'Username cannot be empty'}), 400

    user_id = session['user_id']
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE LOWER(username) = LOWER(?) AND id != ?', (new_username, user_id))
        if cursor.fetchone():
            return jsonify({'error': 'Username is already taken'}), 400

        cursor.execute('UPDATE users SET username = ? WHERE id = ?', (new_username, user_id))
        conn.commit()
        session['username'] = new_username
        return jsonify({'status': 'success', 'username': new_username})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'status': 'success'})

@app.route('/api/me')
def get_current_user():
    if 'user_id' not in session:
        return jsonify({'logged_in': False}), 401

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT username, avatar FROM users WHERE id = ?', (session['user_id'],))
        row = cursor.fetchone()
        avatar = row[1] if row and row[1] else '🧩'
        username = row[0] if row else session['username']

    return jsonify({'logged_in': True, 'username': username, 'avatar': avatar})


@app.route('/api/change-avatar', methods=['POST'])
def change_avatar():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    avatar = data.get('avatar', '🧩')
    user_id = session['user_id']

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET avatar = ? WHERE id = ?', (avatar, user_id))
        conn.commit()
        session['avatar'] = avatar
        return jsonify({'status': 'success', 'avatar': avatar})

# Leaderboard Endpoint for Friends Tab
@app.route('/api/friends/leaderboard')
def get_friends_leaderboard():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    puzzle_type = request.args.get('type', 'normal')  # 'normal', 'midi', 'mini', 'wordle'
    date_str = request.args.get('date')               # 'YYYY-MM-DD'

    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')

    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        year = str(dt.year)
        month = f"{dt.month:02d}"
        day_base = f"{dt.day:02d}"
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400

    config = TYPE_FOLDER_CONFIG.get(puzzle_type, TYPE_FOLDER_CONFIG['normal'])
    day_key = day_base + config['suffix']
    publisher = config['publisher']

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.id, u.username, u.avatar,
                   p.timer_seconds, p.completed, p.used_check, p.ratio
            FROM users u
            LEFT JOIN progress p ON u.id = p.user_id
                AND p.publisher = ?
                AND p.year = ?
                AND p.month = ?
                AND p.day_key = ?
            ORDER BY
                CASE WHEN p.completed = 1 THEN 1 ELSE 2 END,
                p.timer_seconds ASC,
                u.username ASC
        ''', (publisher, year, month, day_key))

        results = []
        for row in cursor.fetchall():
            results.append({
                'userId': row[0],
                'username': row[1],
                'avatar': row[2] or '🧩',
                'timerSeconds': row[3],
                'completed': bool(row[4]) if row[4] is not None else False,
                'usedCheck': bool(row[5]) if row[5] is not None else False,
                'ratio': row[6] if row[6] is not None else 0.0,
                'hasStarted': row[3] is not None
            })

        return jsonify(results)

# User Solve Time Statistics Endpoint
@app.route('/api/user/stats')
def get_user_stats():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT publisher, year, month, day_key, timer_seconds, used_check
            FROM progress
            WHERE user_id = ? AND completed = 1
        ''', (user_id,))

        mini_times = []
        midi_times = []
        wordle_win_guesses = []   # number of guesses used, only for won games
        wordle_guess_dist = {str(n): 0 for n in range(1, WORDLE_MAX_GUESSES + 1)}
        wordle_wins = 0
        wordle_total = 0
        crossword_times = {
            'Monday': [], 'Tuesday': [], 'Wednesday': [],
            'Thursday': [], 'Friday': [], 'Saturday': [], 'Sunday': []
        }

        for row in cursor.fetchall():
            publisher, year, month, day_key, secs, used_check = row

            if publisher == WORDLE_PUBLISHER:
                wordle_total += 1
                if used_check:
                    wordle_wins += 1
                    # secs is repurposed for Wordle: number of guesses used to win
                    if secs is not None and 1 <= secs <= WORDLE_MAX_GUESSES:
                        wordle_win_guesses.append(secs)
                        wordle_guess_dist[str(secs)] += 1
                continue

            if secs is None or secs <= 0:
                continue

            if day_key.endswith('-mini'):
                mini_times.append(secs)
            elif day_key.endswith('-midi'):
                midi_times.append(secs)
            else:
                try:
                    day_num = int(day_key)
                    dt = datetime(int(year), int(month), day_num)
                    day_name = dt.strftime('%A')
                    if day_name in crossword_times:
                        crossword_times[day_name].append(secs)
                except Exception:
                    pass

        def calc_avg(arr):
            return round(sum(arr) / len(arr)) if arr else None

        def calc_avg_float(arr):
            return round(sum(arr) / len(arr), 1) if arr else None

        stats = {
            'miniAvg': calc_avg(mini_times),
            'miniCount': len(mini_times),
            'midiAvg': calc_avg(midi_times),
            'midiCount': len(midi_times),
            'wordleAvg': calc_avg_float(wordle_win_guesses),  # average guesses per win
            'wordleCount': wordle_total,
            'wordleWinRate': round((wordle_wins / wordle_total) * 100) if wordle_total else None,
            'wordleGuessDistribution': wordle_guess_dist,      # {"1": count, ..., "6": count}, wins only
            'crosswordByDay': {
                day: {
                    'avg': calc_avg(times),
                    'count': len(times)
                }
                for day, times in crossword_times.items()
            }
        }
        return jsonify(stats)

# Session-Secured Progress APIs
@app.route('/api/progress', methods=['GET'])
def get_user_progress_summary():
    if 'user_id' not in session:
        return jsonify({}), 401

    user_id = session['user_id']
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT publisher, year, month, day_key, ratio, completed, used_check, timer_seconds
            FROM progress WHERE user_id = ?
        ''', (user_id,))
        summary = {}
        for row in cursor.fetchall():
            key = f"{row[0]}/{row[1]}/{row[2]}/{row[3]}"
            summary[key] = {
                'ratio': row[4],
                'completed': bool(row[5]),
                'usedCheck': bool(row[6]),
                # For crosswords this is elapsed solve time in seconds.
                # For Wordle it's repurposed to hold the number of guesses used.
                'timerSeconds': row[7]
            }
        return jsonify(summary)

@app.route('/api/progress/<publisher>/<year>/<month>/<path:day_key>', methods=['GET', 'POST'])
def handle_puzzle_progress(publisher, year, month, day_key):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if request.method == 'POST':
            data = request.json
            cursor.execute('''
                INSERT INTO progress (user_id, publisher, year, month, day_key, user_grid, timer_seconds, ratio, completed, used_check)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, publisher, year, month, day_key) DO UPDATE SET
                    user_grid = excluded.user_grid,
                    timer_seconds = excluded.timer_seconds,
                    ratio = excluded.ratio,
                    completed = excluded.completed,
                    used_check = excluded.used_check
            ''', (
                user_id, publisher, year, month, day_key,
                json.dumps(data.get('userGrid', [])),
                data.get('timerSeconds', 0),
                data.get('ratio', 0.0),
                1 if data.get('completed') else 0,
                1 if data.get('usedCheck') else 0
            ))
            conn.commit()
            return jsonify({'status': 'success'})
        else:
            cursor.execute('''
                SELECT user_grid, timer_seconds, ratio, completed, used_check
                FROM progress WHERE user_id = ? AND publisher = ? AND year = ? AND month = ? AND day_key = ?
            ''', (user_id, publisher, year, month, day_key))
            row = cursor.fetchone()
            if row:
                return jsonify({
                    'userGrid': json.loads(row[0]),
                    'timerSeconds': row[1],
                    'ratio': row[2],
                    'completed': bool(row[3]),
                    'usedCheck': bool(row[4])
                })
            return jsonify({})

# Puzzle type -> (publisher, day_key suffix). For the NY Times puzzles, the
# publisher is always 'NY Times' and the puzzle type is encoded as a suffix
# on the day_key. Wordle isn't from NY Times, so it gets its own publisher
# instead of a suffix, but otherwise reuses the exact same progress schema.
PUZZLE_PUBLISHER = 'NY Times'
WORDLE_PUBLISHER = 'Wordle'
SPELLING_BEE_PUBLISHER = 'SpellingBee'
TYPE_FOLDER_CONFIG = {
    'normal':       {'publisher': PUZZLE_PUBLISHER, 'suffix': '',},
    'midi':         {'publisher': PUZZLE_PUBLISHER, 'suffix': '-midi',},
    'mini':         {'publisher': PUZZLE_PUBLISHER, 'suffix': '-mini',},
    'wordle':       {'publisher': WORDLE_PUBLISHER, 'suffix': '',},
    'spelling-bee': {'publisher': SPELLING_BEE_PUBLISHER, 'suffix': '',},
}

@app.route('/archive')
@app.route('/archive/<type_folder>')
def archive(type_folder='midi'):
    if 'user_id' not in session:
        return redirect('/login')
    if type_folder not in TYPE_FOLDER_CONFIG:
        type_folder = 'midi'
    return render_template('archive.html', initial_type=type_folder)

@app.route('/api/archive-progress/<type_folder>/<year>/<month>')
def get_archive_progress(type_folder, year, month):
    if 'user_id' not in session:
        return jsonify({'progress': {}}), 401
    if type_folder not in TYPE_FOLDER_CONFIG:
        return jsonify({'error': 'Invalid puzzle type'}), 400

    config = TYPE_FOLDER_CONFIG[type_folder]
    suffix = config['suffix']
    publisher = config['publisher']
    user_id = session['user_id']
    month_padded = str(int(month)).zfill(2)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT day_key, ratio, completed, used_check
            FROM progress
            WHERE user_id = ? AND publisher = ? AND year = ? AND month = ?
        ''', (user_id, publisher, str(year), month_padded))

        progress = {}
        for day_key, ratio, completed, used_check in cursor.fetchall():
            if suffix:
                if not day_key.endswith(suffix):
                    continue
                day_part = day_key[:-len(suffix)]
            else:
                day_part = day_key

            if not day_part.isdigit():
                continue

            date_key = f"{year}-{month_padded}-{day_part.zfill(2)}"
            progress[date_key] = {
                'ratio': ratio,
                'completed': bool(completed),
                'usedCheck': bool(used_check),
            }

    # Which days this month actually have a puzzle at all. Only meaningful
    # for the NY Times types - old years didn't publish every day, and the
    # Midi/Mini didn't exist yet, so plenty of days in-range still have no
    # puzzle. Wordle's availability is contiguous and already handled via
    # /api/wordle-range, so we skip the (pointless) lookup for it. Spelling
    # Bee is one JSON file per day, so it gets its own directory scan.
    available_days = None
    if type_folder == 'spelling-bee':
        available_days = get_spelling_bee_days_in_month(year, month_padded)
    elif type_folder != 'wordle':
        available_days = get_puzzle_days_in_month(publisher, suffix, year, month_padded)

    return jsonify({'progress': progress, 'availableDays': available_days})

def get_spelling_bee_days_in_month(year, month_padded):
    """Days (as ints) within a given year/month that have a Spelling Bee
    puzzle file on disk, mirroring get_puzzle_days_in_month() but scanning
    puzzles/spelling-bee/<year>/<month>/ instead of the master index."""
    folder = os.path.join(SPELLING_BEE_DIR, str(int(year)), month_padded)
    if not os.path.isdir(folder):
        return []
    days = []
    for fname in os.listdir(folder):
        if not fname.endswith('.json'):
            continue
        day_part = fname[:-len('.json')]
        if day_part.isdigit():
            days.append(int(day_part))
    return sorted(days)

def get_spelling_bee_available_range():
    """Earliest (year, month) with at least one Spelling Bee puzzle file on
    disk, or None if there aren't any yet."""
    if not os.path.isdir(SPELLING_BEE_DIR):
        return None
    min_ym = None
    for year_str in os.listdir(SPELLING_BEE_DIR):
        year_path = os.path.join(SPELLING_BEE_DIR, year_str)
        if not (year_str.isdigit() and os.path.isdir(year_path)):
            continue
        for month_str in os.listdir(year_path):
            month_path = os.path.join(year_path, month_str)
            if not (month_str.isdigit() and os.path.isdir(month_path)):
                continue
            if any(f.endswith('.json') for f in os.listdir(month_path)):
                ym = (int(year_str), int(month_str))
                if min_ym is None or ym < min_ym:
                    min_ym = ym
    return min_ym

def get_puzzle_days_in_month(publisher, suffix, year, month_padded):
    """Days (as ints) within a given year/month that have a puzzle indexed
    for this publisher/suffix. The master index is loaded once and cached
    in memory, so this is just a handful of dict lookups per call - cheap
    even for sparse, decades-old months."""
    try:
        master = get_master_index()
        days_dict = master.get(publisher, {}).get(str(year), {}).get(month_padded, {})
    except Exception:
        days_dict = {}

    days = set()
    for day_key in days_dict.keys():
        if suffix:
            if not day_key.endswith(suffix):
                continue
            day_part = day_key[:-len(suffix)]
        else:
            day_part = day_key
        if day_part.isdigit():
            days.add(int(day_part))

    # Merge in anything sitting in the scraped-puzzle fallback folder too,
    # so days that only exist there still show up (un-greyed) on the calendar.
    days.update(get_fallback_puzzle_days(publisher, suffix, year, month_padded))

    return sorted(days)

# Available date range for the Wordle archive, so the calendar can grey out
# days that fall before the word list started (in addition to future days).
@app.route('/api/wordle-range')
def wordle_range():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    start, end = get_wordle_available_range()
    if start is None or end is None:
        return jsonify({'startDate': None, 'endDate': None})
    return jsonify({'startDate': start.isoformat(), 'endDate': end.isoformat()})

_ARCHIVE_RANGE_CACHE = {}

def get_puzzle_available_range(type_folder):
    """Returns ((min_year, min_month), (max_year, max_month)) spanning every
    month that has at least one puzzle for this archive type, or
    (None, None) if nothing is indexed. Each puzzle type can have a very
    different starting point - the classic Crossword goes back to 1942,
    while the Midi and Mini are much newer - so this is computed per type
    rather than per publisher. Cached since the master index doesn't
    change during the life of the process."""
    if type_folder in _ARCHIVE_RANGE_CACHE:
        return _ARCHIVE_RANGE_CACHE[type_folder]

    config = TYPE_FOLDER_CONFIG.get(type_folder)
    if not config:
        return None, None

    publisher = config['publisher']
    suffix = config['suffix']

    try:
        master = get_master_index()
    except Exception:
        return None, None

    pub_data = master.get(publisher, {})
    min_ym = None
    max_ym = None
    for year_str, months in pub_data.items():
        if not str(year_str).isdigit():
            continue
        year = int(year_str)
        for month_str, days in months.items():
            if not str(month_str).isdigit():
                continue
            month = int(month_str)
            matched = False
            for day_key in days.keys():
                if suffix:
                    if day_key.endswith(suffix):
                        matched = True
                        break
                else:
                    if day_key.isdigit():
                        matched = True
                        break
            if matched:
                ym = (year, month)
                if min_ym is None or ym < min_ym:
                    min_ym = ym
                if max_ym is None or ym > max_ym:
                    max_ym = ym

    _ARCHIVE_RANGE_CACHE[type_folder] = (min_ym, max_ym)
    return min_ym, max_ym

# Earliest year+month with available puzzles for a given archive tab, so the
# month/year picker doesn't let people scroll back past when that puzzle
# type actually started (each type started at a different point).
@app.route('/api/archive-range/<type_folder>')
def archive_range(type_folder):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if type_folder not in TYPE_FOLDER_CONFIG:
        return jsonify({'error': 'Invalid puzzle type'}), 400

    now = datetime.now()

    if type_folder == 'wordle':
        start, _ = get_wordle_available_range()
        start_year = start.year if start else None
        start_month = start.month if start else None
    elif type_folder == 'spelling-bee':
        min_ym = get_spelling_bee_available_range()
        if min_ym:
            start_year, start_month = min_ym
        else:
            start_year, start_month = None, None
    else:
        min_ym, _ = get_puzzle_available_range(type_folder)
        if min_ym:
            start_year, start_month = min_ym
        else:
            start_year, start_month = None, None

    if start_year is None:
        start_year, start_month = now.year, now.month

    return jsonify({
        'startYear': start_year,
        'startMonth': start_month,  # 1-indexed
        'endYear': now.year,
    })

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect('/login')

    today = datetime.now()
    year = str(today.year)
    month = f"{today.month:02d}"
    day_base = f"{today.day:02d}"
    today_date = today.date()

    today_titles = {}
    # Whether each type's *today* entry has actually been scraped yet. The
    # scrapers fire on cron schedules in America/New_York (docker/crontab)
    # while this container's clock is on a different TZ (see
    # docker-compose.yml), so "today" here can roll over before/after the
    # scraper has written that day's file. Checking the data directly stays
    # correct regardless of either timezone.
    today_available = {}
    master = get_master_index()
    meta = get_data(0, 22, 78)
    header = get_data(*meta[5:8], mode='raw')

    for p_type, cfg in TYPE_FOLDER_CONFIG.items():
        if p_type == 'wordle':
            today_available[p_type] = get_wordle_word_for_date(today_date) is not None
            today_titles[p_type] = "Guess your way to the correct word."
            continue
        if p_type == 'spelling-bee':
            today_available[p_type] = get_spelling_bee_puzzle(today_date) is not None
            today_titles[p_type] = "Make as many words as you can with 7 letters."
            continue

        day_key = day_base + cfg['suffix']
        publisher = cfg['publisher']
        found = False
        title_text = "Crack the clues in today's puzzle."
        try:
            info = master[publisher][year][month][day_key]
            puz_data = get_data(*info, mode='gzip', header=header)
            title_text = f"“{get_puzzle_title(puz_data)}”"
            found = True
        except Exception:
            fallback = load_fallback_puzzle_json(publisher, year, month, day_key)
            if fallback is not None:
                title_text = f"“{fallback.get('title') or 'Crack the clues in todays puzzle.'}”"
                found = True

        today_available[p_type] = found
        today_titles[p_type] = "Solve the puzzle in seconds." if p_type == 'mini' else title_text

    return render_template('index.html', today_titles=today_titles, today_available=today_available)

@app.route('/play/<publisher>/<year>/<month>/<path:day_key>')
def play_puzzle(publisher, year, month, day_key):
    if 'user_id' not in session:
        return redirect('/login')
    return render_template('play.html', publisher=publisher, year=year, month=month, day_key=day_key)

# Wordle Routes
@app.route('/wordle')
def wordle_today():
    if 'user_id' not in session:
        return redirect('/login')
    today = date.today()
    return redirect(f"/wordle/{today.year}/{today.month:02d}/{today.day:02d}")

@app.route('/wordle/<year>/<month>/<day>')
def play_wordle(year, month, day):
    if 'user_id' not in session:
        return redirect('/login')
    try:
        target_date = date(int(year), int(month), int(day))
    except ValueError:
        return "Invalid date", 400

    word = get_wordle_word_for_date(target_date)
    if word is None:
        return render_template('wordle.html', target_word='', word_unavailable=True,
                                year=year, month=f"{int(month):02d}", day=f"{int(day):02d}")

    return render_template('wordle.html', target_word=word, word_unavailable=False,
                            year=year, month=f"{int(month):02d}", day=f"{int(day):02d}",
                            max_guesses=WORDLE_MAX_GUESSES)

@app.route('/api/wordle-valid-words')
def wordle_valid_words():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return Response(get_valid_wordle_words_text(), mimetype='text/plain')

# Spelling Bee Routes

def get_spelling_bee_puzzle(target_date: date):
    """Loads puzzles/spelling-bee/<year>/<month>/<day>.json for a given date,
    or None if that day doesn't have a puzzle on disk."""
    path = os.path.join(
        SPELLING_BEE_DIR, str(target_date.year),
        f"{target_date.month:02d}", f"{target_date.day:02d}.json"
    )
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

@app.route('/spelling-bee')
def spelling_bee_today():
    if 'user_id' not in session:
        return redirect('/login')
    today = date.today()
    return redirect(f"/spelling-bee/{today.year}/{today.month:02d}/{today.day:02d}")

@app.route('/spelling-bee/<year>/<month>/<day>')
def play_spelling_bee(year, month, day):
    if 'user_id' not in session:
        return redirect('/login')
    try:
        target_date = date(int(year), int(month), int(day))
    except ValueError:
        return "Invalid date", 400

    puzzle = get_spelling_bee_puzzle(target_date)
    if puzzle is None:
        return render_template('spelling-bee.html', puzzle_unavailable=True,
                                year=year, month=f"{int(month):02d}", day=f"{int(day):02d}")

    return render_template('spelling-bee.html', puzzle_unavailable=False, puzzle=puzzle,
                            year=year, month=f"{int(month):02d}", day=f"{int(day):02d}")

@app.route('/api/puzzles-list')
def puzzles_list():
    return jsonify(get_master_index())

@app.route('/api/puzzle-json/<publisher>/<year>/<month>/<path:day_key>')
def get_puzzle_json(publisher, year, month, day_key):
    # First choice: the master archive (puzzles/xwords).
    try:
        master = get_master_index()
        info = master[publisher][year][month][day_key]
        meta = get_data(0, 22, 78)
        header = get_data(*meta[5:8], mode='raw')
        puz_data = get_data(*info, mode='gzip', header=header)

        name = f"{publisher} - {year}-{month}-{day_key}"
        return jsonify(build_puzzle_json(name, puz_data))
    except Exception:
        pass  # Not in the master archive - fall through to the scraper's folder.

    # Second choice: an individually-scraped puzzle (puzzles/crosswords).
    try:
        fallback = load_fallback_puzzle_json(publisher, year, month, day_key)
        if fallback is not None:
            return jsonify(fallback)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"error": f"No puzzle found for {publisher}/{year}/{month}/{day_key}"}), 404

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5235)
