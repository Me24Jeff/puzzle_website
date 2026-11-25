from flask import (Flask, render_template, jsonify, request, session, redirect,
                   Response, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from functools import lru_cache
import os, gzip, json, sqlite3, time, hashlib, secrets, math, re
from collections import deque

app = Flask(__name__, static_folder="/app/data/static", static_url_path="/static")

# Nothing the site sends needs more than a few hundred KB (the biggest body is
# a saved crossword grid), so refuse anything bigger before it is even parsed.
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024

# ---------------------------------------------------------------------------
# Secret key + session/cookie configuration
# ---------------------------------------------------------------------------
# The secret key signs the login cookie. If it changes (or differs between
# gunicorn workers) everybody is logged out, so it must be stable. Order:
#   1. $FLASK_SECRET_KEY, if set
#   2. a key generated once and stored in <data dir>/.flask_secret_key
# Nothing secret needs to live in the git repo any more.
SECRET_KEY_FILE = ".flask_secret_key"


def _load_secret_key():
    env_key = os.environ.get('FLASK_SECRET_KEY')
    if env_key:
        return env_key

    try:
        with open(SECRET_KEY_FILE, 'r') as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass

    # First run: create the key atomically so two gunicorn workers starting at
    # the same moment can't end up with different keys.
    key = os.urandom(32).hex()
    tmp = f"{SECRET_KEY_FILE}.{os.getpid()}.tmp"
    with open(tmp, 'w') as f:
        f.write(key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        os.link(tmp, SECRET_KEY_FILE)      # fails if another worker won the race
    except FileExistsError:
        pass
    except OSError:                        # filesystem without hard links
        if not os.path.exists(SECRET_KEY_FILE):
            os.replace(tmp, SECRET_KEY_FILE)
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
    with open(SECRET_KEY_FILE, 'r') as f:
        return f.read().strip()


app.secret_key = _load_secret_key()

# "Remember me": the login cookie is a *permanent* cookie that lasts a year and
# is slid forward whenever the user is active (see keep_session_alive below).
# Without this it is a browser-session cookie, which mobile browsers / iOS home
# screen apps throw away after a short while.
SESSION_LIFETIME = timedelta(days=365)
SESSION_REFRESH_SECONDS = 24 * 60 * 60   # push the expiry forward at most once a day

app.config.update(
    PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    # We refresh manually (once a day) instead of re-sending Set-Cookie on
    # every single response, including static files.
    SESSION_REFRESH_EACH_REQUEST=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Only set COOKIE_SECURE=1 if you serve the site over HTTPS; a Secure
    # cookie is silently dropped on plain http:// and nobody could log in.
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'),
    # Static files (icons/SVGs/etc.) may be cached by the browser for a week
    # instead of being re-validated on every page view.
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
)

PUZZLE_DIR = "puzzles/xwords"
DB_PATH = "puzzle_progress.db"

# Admins are configured with a comma-separated list of usernames, e.g.
#   ADMIN_USERNAMES=alice,bob
# The list is applied at startup (see init_db): those users get is_admin=1 and
# everybody else gets 0. With the variable unset there are no admins at all.
ADMIN_USERNAMES = {u.strip().lower() for u in os.environ.get('ADMIN_USERNAMES', '').split(',') if u.strip()}

# Spelling Bee "Genius" rank starts at this fraction of the max points.
BEE_GENIUS_RATIO = 0.70

# Fallback folder for individually-scraped crosswords (see crossword_scraper.py).
# PUZZLE_DIR/the master index is a pre-baked historical archive that isn't meant
# to be appended to day-by-day. Instead, freshly-scraped puzzles are dropped in
# here as one small JSON file per puzzle, already in the exact shape
# build_puzzle_json() below produces. Any time a puzzle can't be found in the
# master archive, we check here before giving up.
CROSSWORD_FALLBACK_DIR = "puzzles/crosswords"

# Fixed-width word list for Wordle ("YYYY-MM-DD WORD\n", 17 bytes per record).
WORDLE_WORDS_FILE = "puzzles/wordle_words.txt"
WORDLE_MAX_GUESSES = 6

# Plain-text dictionary of every word players are allowed to guess (not just
# possible answers). One word per line, alphabetically sorted. Served to the
# client so it can validate guesses before accepting them.
WORDLE_VALID_WORDS_FILE = "puzzles/valid_wordle_words.txt"

# One JSON file per day. Layout: puzzles/spelling-bee/<year>/<month>/<day>.json
SPELLING_BEE_DIR = "puzzles/spelling-bee"

# One JSON file per day. Layout: puzzles/connections/<year>/<month>/<day>.json
CONNECTIONS_DIR = "puzzles/connections"

# Puzzle type -> (publisher, day_key suffix). For the NY Times puzzles, the
# publisher is always 'NY Times' and the puzzle type is encoded as a suffix
# on the day_key. Wordle, Spelling Bee, and Connections get their own
# publisher instead of a suffix, reusing the exact same progress schema.
PUZZLE_PUBLISHER = 'NY Times'
WORDLE_PUBLISHER = 'Wordle'
SPELLING_BEE_PUBLISHER = 'SpellingBee'
CONNECTIONS_PUBLISHER = 'Connections'
TYPE_FOLDER_CONFIG = {
    'normal':       {'publisher': PUZZLE_PUBLISHER, 'suffix': '',},
    'midi':         {'publisher': PUZZLE_PUBLISHER, 'suffix': '-midi',},
    'mini':         {'publisher': PUZZLE_PUBLISHER, 'suffix': '-mini',},
    'wordle':       {'publisher': WORDLE_PUBLISHER, 'suffix': '',},
    'spelling-bee': {'publisher': SPELLING_BEE_PUBLISHER, 'suffix': '',},
    'connections':  {'publisher': CONNECTIONS_PUBLISHER, 'suffix': '',},
}

for _d in (PUZZLE_DIR, CROSSWORD_FALLBACK_DIR, SPELLING_BEE_DIR, CONNECTIONS_DIR):
    os.makedirs(_d, exist_ok=True)


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------
USERNAME_MAX_LEN = 32
PASSWORD_MIN_LEN = 6
PASSWORD_MAX_LEN = 128      # scrypt cost grows with input size; don't hash megabytes
AVATAR_MAX_LEN = 16         # a few emoji (ZWJ sequences are long-ish), never a paragraph
PROGRESS_GRID_MAX_BYTES = 512 * 1024

# Publisher / day-key / year / month as they appear in URLs and in the
# progress table. Deliberately has no "/", "\" or leading "." so a value can
# never walk out of a folder when it is used to build a file path.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _&'-]{0,63}$")
_SAFE_DAY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")


def json_body():
    """The request's JSON object, or {} if the body is missing / not an object."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def str_field(data, key, default=''):
    """data[key] if it is a string, else `default` (never raises on odd JSON types)."""
    v = data.get(key, default)
    return v if isinstance(v, str) else default


def valid_username(name):
    """Returns an error message, or None if the username is fine."""
    if not name:
        return 'Username cannot be empty'
    if len(name) > USERNAME_MAX_LEN:
        return f'Username must be at most {USERNAME_MAX_LEN} characters'
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return 'Username contains invalid characters'
    return None


def valid_password(pw):
    if len(pw) < PASSWORD_MIN_LEN:
        return f'Password must be at least {PASSWORD_MIN_LEN} characters'
    if len(pw) > PASSWORD_MAX_LEN:
        return f'Password must be at most {PASSWORD_MAX_LEN} characters'
    return None


def valid_progress_key(publisher, year, month, day_key):
    return bool(_SAFE_NAME_RE.match(publisher) and _YEAR_RE.match(year)
                and _MONTH_RE.match(month) and _SAFE_DAY_KEY_RE.match(day_key))


# --- Login throttling --------------------------------------------------------
# A small in-memory brake on password guessing: after LOGIN_MAX_FAILURES wrong
# passwords for one username within LOGIN_WINDOW_SECONDS, further attempts for
# that username are refused until the window slides past. (Per gunicorn worker,
# which is plenty to turn "thousands of guesses a minute" into a trickle.)
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 10 * 60
_login_failures = {}          # lower-cased username -> deque of failure timestamps


def _recent_failures(key, now):
    q = _login_failures.get(key)
    if not q:
        return 0
    while q and now - q[0] > LOGIN_WINDOW_SECONDS:
        q.popleft()
    if not q:
        _login_failures.pop(key, None)
        return 0
    return len(q)


def login_throttled(username):
    return _recent_failures(username.lower(), time.time()) >= LOGIN_MAX_FAILURES


def note_login_failure(username):
    now = time.time()
    if len(_login_failures) > 5000:          # bound memory against username-spraying
        for k in list(_login_failures):
            _recent_failures(k, now)
        if len(_login_failures) > 5000:
            _login_failures.clear()
    _login_failures.setdefault(username.lower(), deque()).append(now)


def clear_login_failures(username):
    _login_failures.pop(username.lower(), None)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
@contextmanager
def db():
    """Open a connection, commit on success, and *always* close it.
    (`with sqlite3.connect(...)` alone commits but never closes.)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute('PRAGMA synchronous=NORMAL')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        cursor = conn.cursor()
        # WAL lets readers and the (frequent) progress writes not block each
        # other across the two gunicorn workers.
        try:
            cursor.execute('PRAGMA journal_mode=WAL')
        except sqlite3.DatabaseError:
            pass

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
        if 'is_admin' not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        if 'must_change_password' not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")

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

        cursor.execute("PRAGMA table_info(progress)")
        if 'auto_check' not in [c[1] for c in cursor.fetchall()]:
            cursor.execute("ALTER TABLE progress ADD COLUMN auto_check INTEGER DEFAULT 0")

        # Make the admin flag mirror ADMIN_USERNAMES exactly.
        cursor.execute('UPDATE users SET is_admin = 0')
        for name in ADMIN_USERNAMES:
            cursor.execute('UPDATE users SET is_admin = 1 WHERE LOWER(username) = ?', (name,))


init_db()


def get_user_info(user_id):
    """{'username', 'avatar', 'isAdmin', 'mustChange'} for a user id, or None if the user is gone."""
    with db() as conn:
        row = conn.execute(
            'SELECT username, avatar, is_admin, must_change_password FROM users WHERE id = ?', (user_id,)
        ).fetchone()
    if not row:
        return None
    return {'username': row[0], 'avatar': row[1] or '🧩',
            'isAdmin': bool(row[2]), 'mustChange': bool(row[3])}


def is_admin(user_id):
    with db() as conn:
        row = conn.execute('SELECT is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
    return bool(row and row[0])


def get_progress_summary(user_id, since=None):
    """Lightweight per-day progress summary (no grids). `since` is an
    'YYYY-MM-DD' string; when given, only days on/after it are returned, so
    the dashboard doesn't download the user's entire history."""
    sql = '''
        SELECT publisher, year, month, day_key, ratio, completed, used_check, timer_seconds
        FROM progress WHERE user_id = ?
    '''
    params = [user_id]
    if since:
        sql += " AND (year || '-' || month || '-' || substr(day_key, 1, 2)) >= ?"
        params.append(since)

    summary = {}
    with db() as conn:
        for row in conn.execute(sql, params):
            key = f"{row[0]}/{row[1]}/{row[2]}/{row[3]}"
            summary[key] = {
                'ratio': row[4],
                'completed': bool(row[5]),
                'usedCheck': bool(row[6]),
                # For crosswords this is elapsed solve time in seconds.
                # For Wordle it's repurposed to hold the number of guesses used.
                # For Spelling Bee it holds the score.
                # For Connections it holds the number of mistakes made (0-4).
                'timerSeconds': row[7]
            }
    return summary


def get_saved_progress(user_id, publisher, year, month, day_key):
    """The saved state for one puzzle (same shape the progress API returns)."""
    with db() as conn:
        row = conn.execute('''
            SELECT user_grid, timer_seconds, ratio, completed, used_check, auto_check
            FROM progress WHERE user_id = ? AND publisher = ? AND year = ? AND month = ? AND day_key = ?
        ''', (user_id, publisher, year, month, day_key)).fetchone()
    if not row:
        return {}
    try:
        grid = json.loads(row[0])
    except (TypeError, ValueError):
        grid = []
    return {
        'userGrid': grid,
        'timerSeconds': row[1],
        'ratio': row[2],
        'completed': bool(row[3]),
        'usedCheck': bool(row[4]),
        'autoCheck': bool(row[5]),
    }


# ---------------------------------------------------------------------------
# Master puzzle archive (puzzles/xwords)
# ---------------------------------------------------------------------------
MASTER_INDEX = None
_HEADER = None


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
    # Read only the bytes we need. (This used to read the whole multi-MB .dat
    # file into memory and then slice a few KB out of it, on every request.)
    try:
        with open(filename, 'rb') as f:
            f.seek(start)
            data = f.read(length)
    except FileNotFoundError:
        raise FileNotFoundError(f"Puzzle data file {filename} not found locally.")

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


def get_header():
    """Shared gzip header for every puzzle in the archive - read once."""
    global _HEADER
    if _HEADER is None:
        meta = get_data(0, 22, 78)
        _HEADER = get_data(*meta[5:8], mode='raw')
    return _HEADER


def get_master_index():
    global MASTER_INDEX
    if MASTER_INDEX is None:
        meta = get_data(0, 22, 78)
        raw_index = get_data(*meta[2:5], mode='gzip', header=get_header())
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


DEFAULT_CROSSWORD_BLURB = "Crack the clues in today's puzzle."


def get_puzzle_title(puz_data):
    meta = puz_data[6] if len(puz_data) > 6 else {}
    return meta.get("title") or DEFAULT_CROSSWORD_BLURB


@lru_cache(maxsize=256)
def _load_master_puzzle(publisher, year, month, day_key):
    """Puzzle from the historical archive. These never change, so the built
    JSON is memoised (exceptions - i.e. 'not in the archive' - aren't cached)."""
    master = get_master_index()
    info = master[publisher][year][month][day_key]
    puz_data = get_data(*info, mode='gzip', header=get_header())
    return build_puzzle_json(f"{publisher} - {year}-{month}-{day_key}", puz_data)


_TITLE_CACHE = {}


def _master_title(publisher, year, month, day_key):
    key = (publisher, year, month, day_key)
    if key in _TITLE_CACHE:
        return _TITLE_CACHE[key]
    master = get_master_index()
    info = master[publisher][year][month][day_key]
    puz_data = get_data(*info, mode='gzip', header=get_header())
    title = get_puzzle_title(puz_data)
    if len(_TITLE_CACHE) > 512:
        _TITLE_CACHE.clear()
    _TITLE_CACHE[key] = title
    return title


# --- Fallback folder helpers (scraped puzzles not in the master archive) ---

def fallback_puzzle_path(publisher, year, month, day_key):
    """Path of a scraped puzzle's JSON file. publisher/year/month/day_key come
    straight from the URL, so they are validated hard: anything that could
    escape CROSSWORD_FALLBACK_DIR (".." or slashes) raises ValueError."""
    if not (_SAFE_NAME_RE.match(str(publisher)) and _SAFE_DAY_KEY_RE.match(str(day_key))):
        raise ValueError("invalid puzzle reference")
    path = os.path.join(
        CROSSWORD_FALLBACK_DIR, publisher, str(int(year)), f"{int(month):02d}", f"{day_key}.json"
    )
    # Belt and braces: whatever the regexes allowed, the resolved file must
    # still live inside the fallback folder.
    root = os.path.realpath(CROSSWORD_FALLBACK_DIR)
    if os.path.commonpath([root, os.path.realpath(path)]) != root:
        raise ValueError("invalid puzzle reference")
    return path


def load_fallback_puzzle_json(publisher, year, month, day_key):
    """Returns the pre-built puzzle JSON for a scraped puzzle, or None if
    there isn't one on disk (or the reference / file is invalid)."""
    try:
        path = fallback_puzzle_path(publisher, year, month, day_key)
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        # ValueError covers bad references AND a corrupt/partial JSON file.
        return None
    return None


def load_puzzle(publisher, year, month, day_key):
    """Returns (puzzle_json_or_None, is_immutable). Master-archive puzzles are
    immutable (safe to cache for a long time); scraped ones can be rewritten."""
    try:
        return _load_master_puzzle(publisher, year, month, day_key), True
    except Exception:
        pass  # Not in the master archive - fall through to the scraper's folder.
    return load_fallback_puzzle_json(publisher, year, month, day_key), False


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


# ---------------------------------------------------------------------------
# Wordle word-list helpers
# ---------------------------------------------------------------------------
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
_VALID_WORDLE_WORDS_ETAG = None


def get_valid_wordle_words_text():
    """Reads the newline-delimited valid-guess dictionary and caches it in
    memory so we don't hit disk on every request."""
    global _VALID_WORDLE_WORDS_CACHE, _VALID_WORDLE_WORDS_ETAG
    if _VALID_WORDLE_WORDS_CACHE is None:
        if os.path.exists(WORDLE_VALID_WORDS_FILE):
            with open(WORDLE_VALID_WORDS_FILE, "r", encoding="utf-8") as f:
                _VALID_WORDLE_WORDS_CACHE = f.read()
        else:
            _VALID_WORDLE_WORDS_CACHE = ""
        _VALID_WORDLE_WORDS_ETAG = hashlib.md5(_VALID_WORDLE_WORDS_CACHE.encode('utf-8')).hexdigest()
    return _VALID_WORDLE_WORDS_CACHE


# ---------------------------------------------------------------------------
# Per-request hooks
# ---------------------------------------------------------------------------
_NO_SESSION_ENDPOINTS = ('static', 'service_worker')


@app.before_request
def keep_session_alive():
    """Make logins persistent: upgrade any old browser-session cookie to a
    year-long permanent one, and slide the expiry forward (at most once a
    day) while the user keeps using the app."""
    if request.endpoint in _NO_SESSION_ENDPOINTS or 'user_id' not in session:
        return
    now = int(time.time())
    if not session.permanent or now - session.get('_t', 0) > SESSION_REFRESH_SECONDS:
        session.permanent = True
        session['_t'] = now


_COMPRESSIBLE = {'text/html', 'text/plain', 'application/json', 'text/css',
                 'application/javascript', 'image/svg+xml'}


@app.after_request
def optimize_response(resp):
    if request.endpoint in _NO_SESSION_ENDPOINTS:
        return resp

    # Cheap hardening headers for every page / API response.
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')

    # HTML pages have per-user data (progress, name, ...) baked in, so they
    # must never come back from the HTTP cache (e.g. on the back button).
    # The same goes for JSON API answers (progress, leaderboard, stats, ...);
    # endpoints that really are cacheable set their own Cache-Control first.
    if resp.mimetype in ('text/html', 'application/json'):
        resp.headers.setdefault('Cache-Control', 'private, no-store')

    # gzip text responses (HTML, JSON, the Wordle dictionary...).
    if (resp.status_code == 200
            and not resp.direct_passthrough
            and resp.mimetype in _COMPRESSIBLE
            and 'Content-Encoding' not in resp.headers
            and 'gzip' in request.headers.get('Accept-Encoding', '')):
        data = resp.get_data()
        if len(data) >= 500:
            resp.set_data(gzip.compress(data, compresslevel=5))
            resp.headers['Content-Encoding'] = 'gzip'
            resp.headers.add('Vary', 'Accept-Encoding')
    return resp


@app.route('/sw.js')
def service_worker():
    """Served from the site root so the worker's scope is the whole app (a
    worker served from /static/ can only control /static/)."""
    resp = send_from_directory(app.root_path, 'sw.js', mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------
def _log_in(user_id):
    session.clear()
    session['user_id'] = user_id
    session.permanent = True
    session['_t'] = int(time.time())


@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect('/')
    return render_template('login.html')


@app.route('/api/register', methods=['POST'])
def register():
    data = json_body()
    username = str_field(data, 'username').strip()
    password = str_field(data, 'password')

    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    err = valid_username(username) or valid_password(password)
    if err:
        return jsonify({'error': err}), 400

    hashed = generate_password_hash(password)
    try:
        with db() as conn:
            cursor = conn.cursor()
            # Check for existing username case-insensitively
            cursor.execute('SELECT id FROM users WHERE LOWER(username) = LOWER(?)', (username,))
            if cursor.fetchone():
                return jsonify({'error': 'Username already taken'}), 400

            cursor.execute('INSERT INTO users (username, password_hash, avatar) VALUES (?, ?, ?)', (username, hashed, '🧩'))
            user_id = cursor.lastrowid
        _log_in(user_id)
        return jsonify({'status': 'success'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Username already taken'}), 400


@app.route('/api/login', methods=['POST'])
def login():
    data = json_body()
    username = str_field(data, 'username').strip()
    password = str_field(data, 'password')

    if login_throttled(username):
        return jsonify({'error': 'Too many failed attempts. Please wait a few minutes and try again.'}), 429
    if len(password) > PASSWORD_MAX_LEN:     # can't be a real password; don't spend a scrypt run on it
        return jsonify({'error': 'Invalid username or password'}), 401

    with db() as conn:
        # Case-insensitive query for username
        user = conn.execute(
            'SELECT id, username, password_hash, avatar FROM users WHERE LOWER(username) = LOWER(?)',
            (username,)
        ).fetchone()

    if user and check_password_hash(user[2], password):
        clear_login_failures(username)
        _log_in(user[0])
        return jsonify({'status': 'success'})
    note_login_failure(username)
    return jsonify({'error': 'Invalid username or password'}), 401


@app.route('/api/change-username', methods=['POST'])
def change_username():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = json_body()
    new_username = str_field(data, 'username').strip()

    err = valid_username(new_username)
    if err:
        return jsonify({'error': err}), 400

    user_id = session['user_id']
    with db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE LOWER(username) = LOWER(?) AND id != ?', (new_username, user_id))
        if cursor.fetchone():
            return jsonify({'error': 'Username is already taken'}), 400

        cursor.execute('UPDATE users SET username = ? WHERE id = ?', (new_username, user_id))
    return jsonify({'status': 'success', 'username': new_username})


@app.route('/api/change-password', methods=['POST'])
def change_password():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = json_body()
    current = str_field(data, 'currentPassword')
    new = str_field(data, 'newPassword')

    if not current or not new:
        return jsonify({'error': 'Current and new password are required'}), 400
    if new == current:
        return jsonify({'error': 'New password must be different from the current one'}), 400
    err = valid_password(new)
    if err:
        return jsonify({'error': err}), 400
    if len(current) > PASSWORD_MAX_LEN:
        return jsonify({'error': 'Current password is incorrect'}), 400

    user_id = session['user_id']
    with db() as conn:
        row = conn.execute('SELECT password_hash FROM users WHERE id = ?', (user_id,)).fetchone()
        if not row or not check_password_hash(row[0], current):
            return jsonify({'error': 'Current password is incorrect'}), 400
        conn.execute(
            'UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?',
            (generate_password_hash(new), user_id)
        )
    return jsonify({'status': 'success'})


# --- Admin: account recovery -------------------------------------------------
# There's no email on file, so "forgot password" is handled by an admin (see
# ADMIN_USERNAMES) generating a one-time temporary password for the user and
# passing it on. The user is nudged to pick their own password after logging in.
_TEMP_PW_ALPHABET = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no look-alike characters


@app.route('/api/admin/users')
def admin_list_users():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if not is_admin(session['user_id']):
        return jsonify({'error': 'Forbidden'}), 403

    with db() as conn:
        rows = conn.execute('SELECT id, username, avatar FROM users ORDER BY LOWER(username)').fetchall()
    return jsonify([{'userId': r[0], 'username': r[1], 'avatar': r[2] or '🧩'} for r in rows])


@app.route('/api/admin/reset-password', methods=['POST'])
def admin_reset_password():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    admin_id = session['user_id']
    if not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403

    data = json_body()
    try:
        target_id = int(data.get('userId'))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': 'Invalid user'}), 400
    if target_id == admin_id:
        return jsonify({'error': "Use 'Change Password' for your own account"}), 400

    temp_password = ''.join(secrets.choice(_TEMP_PW_ALPHABET) for _ in range(10))
    with db() as conn:
        row = conn.execute('SELECT username FROM users WHERE id = ?', (target_id,)).fetchone()
        if not row:
            return jsonify({'error': 'User not found'}), 404
        conn.execute(
            'UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?',
            (generate_password_hash(temp_password), target_id)
        )

    print(f"[admin] user id {admin_id} reset the password of user id {target_id} ({row[0]})", flush=True)
    resp = jsonify({'status': 'success', 'username': row[0], 'tempPassword': temp_password})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'status': 'success'})


@app.route('/api/me')
def get_current_user():
    if 'user_id' not in session:
        return jsonify({'logged_in': False}), 401

    me = get_user_info(session['user_id'])
    if not me:
        session.clear()
        return jsonify({'logged_in': False}), 401
    return jsonify({'logged_in': True, **me})


@app.route('/api/change-avatar', methods=['POST'])
def change_avatar():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = json_body()
    avatar = str_field(data, 'avatar', '🧩').strip()
    # The avatar is shown to every user, so keep it to a short plain string
    # (an emoji) - no markup, no control characters, no essays.
    if (not avatar or len(avatar) > AVATAR_MAX_LEN
            or any(ch in '<>&"\'`' or ord(ch) < 32 or ord(ch) == 127 for ch in avatar)):
        return jsonify({'error': 'Invalid avatar'}), 400
    user_id = session['user_id']

    with db() as conn:
        conn.execute('UPDATE users SET avatar = ? WHERE id = ?', (avatar, user_id))
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

    if publisher == SPELLING_BEE_PUBLISHER:
        # For Spelling Bee timer_seconds holds the score: highest first, people
        # who haven't played (NULL) last.
        order_by = '''CASE WHEN p.timer_seconds IS NULL THEN 1 ELSE 0 END,
                p.timer_seconds DESC,
                u.username COLLATE NOCASE ASC'''
    else:
        # Finished solves first (fastest first), then people still working on
        # it (furthest along first), then people who haven't started. (In
        # SQLite NULLs sort *first* for ASC, which used to put people who hadn't
        # started above those who had.) Wordle's richer win/loss ordering is
        # applied by the dashboard.
        order_by = '''CASE WHEN p.completed = 1 THEN 0
                     WHEN p.timer_seconds IS NOT NULL THEN 1
                     ELSE 2 END,
                CASE WHEN p.completed = 1 THEN p.timer_seconds END ASC,
                p.ratio DESC,
                u.username COLLATE NOCASE ASC'''

    with db() as conn:
        rows = conn.execute(f'''
            SELECT u.id, u.username, u.avatar,
                   p.timer_seconds, p.completed, p.used_check, p.ratio
            FROM users u
            LEFT JOIN progress p ON u.id = p.user_id
                AND p.publisher = ?
                AND p.year = ?
                AND p.month = ?
                AND p.day_key = ?
            ORDER BY
                {order_by}
        ''', (publisher, year, month, day_key)).fetchall()

    results = []
    for row in rows:
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
BEE_RANKS = [("Beginner", 0.00), ("Good Start", 0.02), ("Moving Up", 0.05), ("Good", 0.08),
             ("Solid", 0.15), ("Nice", 0.25), ("Great", 0.40), ("Amazing", 0.50),
             ("Genius", 0.70), ("Queen Bee", 1.00)]


def _bee_rank_index(score, max_points=None, ratio=None):
    # Same rule as the game page: a rank starts at round(pct * max_points) points.
    idx = 0
    for i, (_, pct) in enumerate(BEE_RANKS):
        if max_points:
            reached = score >= math.floor(pct * max_points + 0.5)
        else:
            reached = (ratio or 0) >= pct - 1e-9
        if reached:
            idx = i
    return idx


def _pct(part, whole):
    return round(part * 100 / whole) if whole else None


def _avg(arr, digits=0):
    if not arr:
        return None
    v = sum(arr) / len(arr)
    return round(v, digits) if digits else int(round(v))


@app.route('/api/user/stats')
def get_user_stats():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    with db() as conn:
        rows = conn.execute(
            "SELECT publisher, year, month, day_key, timer_seconds, ratio, completed, used_check, "
            "CASE WHEN publisher IN (?, ?, ?) THEN user_grid ELSE NULL END "
            "FROM progress WHERE user_id = ?",
            (SPELLING_BEE_PUBLISHER, WORDLE_PUBLISHER, CONNECTIONS_PUBLISHER, user_id)
        ).fetchall()

    xw = {k: {'played': 0, 'solved': 0, 'gold': 0, 'times': []} for k in ('normal', 'midi', 'mini')}
    by_day = {d: [] for d in ('Monday', 'Tuesday', 'Wednesday', 'Thursday',
                              'Friday', 'Saturday', 'Sunday')}

    wordle_finished = 0
    wordle_wins = 0
    wordle_hard_wins = 0
    wordle_win_guesses = []
    wordle_dist = {str(n): 0 for n in range(1, WORDLE_MAX_GUESSES + 1)}

    conn_finished = 0
    conn_wins = 0
    conn_perfect = 0
    conn_purple_first = 0
    conn_win_mistakes = []
    conn_dist = {'0': 0, '1': 0, '2': 0, '3': 0, 'loss': 0}

    bee_played = 0
    bee_words = 0
    bee_pangrams = 0
    bee_genius = 0
    bee_queen = 0
    bee_longest = ''
    bee_rank_counts = [0] * len(BEE_RANKS)

    for publisher, year, month, day_key, secs, ratio, completed, used_check, grid_json in rows:
        if publisher == CONNECTIONS_PUBLISHER:
            # For Connections: timer_seconds = mistakes used (0-4), used_check = won.
            if completed:
                conn_finished += 1
                if used_check:
                    conn_wins += 1
                    try:
                        mistakes = int(secs) if secs is not None else 0
                    except (TypeError, ValueError, OverflowError):
                        mistakes = 0
                    mistakes = max(0, min(mistakes, 3))
                    conn_win_mistakes.append(mistakes)
                    conn_dist[str(mistakes)] += 1
                    if mistakes == 0:
                        conn_perfect += 1
                else:
                    conn_dist['loss'] += 1
                try:
                    g = json.loads(grid_json) if grid_json else {}
                except ValueError:
                    g = {}
                if isinstance(g, dict):
                    solved = g.get('solved', [])
                    if isinstance(solved, list) and solved and solved[0] == 3:
                        conn_purple_first += 1
            continue

        if publisher == WORDLE_PUBLISHER:
            # For Wordle: timer_seconds = guesses used, used_check = won.
            if completed:
                wordle_finished += 1
                if used_check:
                    try:
                        g = json.loads(grid_json) if grid_json else {}
                    except ValueError:
                        g = {}
                    if isinstance(g, dict) and g.get('hard'):
                        wordle_hard_wins += 1
                    wordle_wins += 1
                    try:
                        guesses = int(secs)
                    except (TypeError, ValueError, OverflowError):
                        guesses = 0
                    if 1 <= guesses <= WORDLE_MAX_GUESSES:
                        wordle_win_guesses.append(guesses)
                        wordle_dist[str(guesses)] += 1
            continue

        if publisher == SPELLING_BEE_PUBLISHER:
            # For Spelling Bee: timer_seconds = score.
            if not secs or secs <= 0:
                continue
            bee_played += 1

            try:
                puzzle = get_spelling_bee_puzzle(date(int(year), int(month), int(day_key)))
            except Exception:
                puzzle = None
            max_points = puzzle.get('max_points') if puzzle else None
            idx = _bee_rank_index(secs, max_points, ratio)
            bee_rank_counts[idx] += 1
            if idx >= 8:
                bee_genius += 1
            if idx == len(BEE_RANKS) - 1:
                bee_queen += 1

            letters = ({str(puzzle.get('center_letter', '')).upper()} |
                       {str(l).upper() for l in puzzle.get('outer_letters', [])}) if puzzle else set()
            letters.discard('')
            try:
                grid = json.loads(grid_json) if grid_json else {}
            except ValueError:
                grid = {}
            words = grid.get('foundWords', []) if isinstance(grid, dict) else []
            for w in words:
                w = str(w).upper()
                bee_words += 1
                if letters and letters.issubset(set(w)):
                    bee_pangrams += 1
                if len(w) > len(bee_longest):
                    bee_longest = w
            continue

        # Crosswords (NY Times daily / midi / mini)
        if day_key.endswith('-mini'):
            kind = 'mini'
        elif day_key.endswith('-midi'):
            kind = 'midi'
        else:
            kind = 'normal'
        bucket = xw[kind]
        if completed or (ratio or 0) > 0:
            bucket['played'] += 1
        if completed:
            bucket['solved'] += 1
            # "Gold" = solved without any checking. It must only be counted for
            # *solved* puzzles: it used to count every started-but-unsolved
            # puzzle too, so the "no autocheck" rate (gold / solved) could
            # exceed 100%.
            if not used_check:
                bucket['gold'] += 1
            if secs and secs > 0:
                bucket['times'].append(secs)
                if kind == 'normal':
                    try:
                        day_name = datetime(int(year), int(month), int(day_key)).strftime('%A')
                        by_day[day_name].append(secs)
                    except (ValueError, KeyError):
                        pass

    def xw_summary(b):
        return {
            'played': b['played'],
            'solved': b['solved'],
            'solveRate': _pct(b['solved'], b['played']),
            'avgTime': _avg(b['times']),
            'noCheckRate': _pct(b['gold'], b['solved']),
        }

    crossword = xw_summary(xw['normal'])
    crossword['byDay'] = {d: {'avg': _avg(t), 'count': len(t)} for d, t in by_day.items()}

    return jsonify({
        'wordle': {
            'played': wordle_finished,
            'wins': wordle_wins,
            'winRate': _pct(wordle_wins, wordle_finished),
            'avgGuesses': _avg(wordle_win_guesses, 1),
            'distribution': wordle_dist,
            'hardRate': _pct(wordle_hard_wins, wordle_wins),
            'hardWins': wordle_hard_wins,
        },
        'connections': {
            'played': conn_finished,
            'wins': conn_wins,
            'winRate': _pct(conn_wins, conn_finished),
            'perfectGames': conn_perfect,
            'perfectRate': _pct(conn_perfect, conn_wins),
            'avgMistakes': _avg(conn_win_mistakes, 1),
            'purpleFirst': conn_purple_first,
            'distribution': conn_dist,
        },
        'bee': {
            'played': bee_played,
            'genius': bee_genius,
            'queen': bee_queen,
            'totalWords': bee_words,
            'pangrams': bee_pangrams,
            'longestWord': bee_longest or None,
            'rankDistribution': [{'name': BEE_RANKS[i][0], 'count': bee_rank_counts[i]}
                                 for i in range(len(BEE_RANKS))],
        },
        'mini': xw_summary(xw['mini']),
        'midi': xw_summary(xw['midi']),
        'normal': crossword,
    })


# Session-Secured Progress APIs
@app.route('/api/progress', methods=['GET'])
def get_user_progress_summary():
    if 'user_id' not in session:
        return jsonify({}), 401

    since = None
    days = request.args.get('days', type=int)
    if days:
        days = max(1, min(days, 3660))
        since = (date.today() - timedelta(days=days)).isoformat()
    return jsonify(get_progress_summary(session['user_id'], since))


@app.route('/api/progress/<publisher>/<year>/<month>/<path:day_key>', methods=['GET', 'POST'])
def handle_puzzle_progress(publisher, year, month, day_key):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if not valid_progress_key(publisher, year, month, day_key):
        return jsonify({'error': 'Invalid puzzle reference'}), 400

    user_id = session['user_id']
    if request.method == 'POST':
        data = json_body()
        try:
            grid_json = json.dumps(data.get('userGrid', []), separators=(',', ':'))
            timer_seconds = int(max(0, min(float(data.get('timerSeconds', 0) or 0), 10_000_000)))
            ratio = float(data.get('ratio', 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return jsonify({'error': 'Invalid progress data'}), 400
        if len(grid_json) > PROGRESS_GRID_MAX_BYTES:
            return jsonify({'error': 'Progress data too large'}), 413
        if not math.isfinite(ratio):
            return jsonify({'error': 'Invalid progress data'}), 400
        ratio = max(0.0, min(ratio, 1.0))
        with db() as conn:
            conn.execute('''
                INSERT INTO progress (user_id, publisher, year, month, day_key, user_grid, timer_seconds, ratio, completed, used_check, auto_check)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, publisher, year, month, day_key) DO UPDATE SET
                    user_grid = excluded.user_grid,
                    timer_seconds = excluded.timer_seconds,
                    ratio = excluded.ratio,
                    completed = excluded.completed,
                    used_check = excluded.used_check,
                    auto_check = excluded.auto_check
            ''', (
                user_id, publisher, year, month, day_key,
                grid_json,
                timer_seconds,
                ratio,
                1 if data.get('completed') else 0,
                1 if data.get('usedCheck') else 0,
                1 if data.get('autoCheck') else 0
            ))
        return jsonify({'status': 'success'})

    return jsonify(get_saved_progress(user_id, publisher, year, month, day_key))


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
def _get_dated_json_days_in_month(base_dir, year, month_padded):
    """Days (as ints) within a given year/month that have a <DD>.json file
    under `base_dir/<year>/<month>/`."""
    folder = os.path.join(base_dir, str(int(year)), month_padded)
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


def _get_dated_json_available_range(base_dir):
    """Earliest (year, month) with at least one puzzle file on disk under
    `base_dir`, or None if there aren't any yet."""
    if not os.path.isdir(base_dir):
        return None
    min_ym = None
    for year_str in os.listdir(base_dir):
        year_path = os.path.join(base_dir, year_str)
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


def get_spelling_bee_days_in_month(year, month_padded):
    return _get_dated_json_days_in_month(SPELLING_BEE_DIR, year, month_padded)


def get_spelling_bee_available_range():
    return _get_dated_json_available_range(SPELLING_BEE_DIR)


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


def get_archive_range_info(type_folder):
    """Earliest year+month with available puzzles for a given archive tab, so
    the month/year picker doesn't let people scroll back past when that
    puzzle type actually started."""
    now = datetime.now()

    if type_folder == 'wordle':
        start, _ = get_wordle_available_range()
        start_year = start.year if start else None
        start_month = start.month if start else None
    elif type_folder == 'spelling-bee':
        min_ym = get_spelling_bee_available_range()
        start_year, start_month = min_ym if min_ym else (None, None)
    elif type_folder == 'connections':
        min_ym = _get_dated_json_available_range(CONNECTIONS_DIR)
        start_year, start_month = min_ym if min_ym else (None, None)
    else:
        min_ym, _ = get_puzzle_available_range(type_folder)
        start_year, start_month = min_ym if min_ym else (None, None)

    if start_year is None:
        start_year, start_month = now.year, now.month

    return {
        'startYear': start_year,
        'startMonth': start_month,  # 1-indexed
        'endYear': now.year,
    }


def build_archive_bundle(user_id, type_folder, year, month):
    """Everything the archive calendar needs for one type + month, in one go:
    the user's progress, which days have puzzles, the type's start month and
    (for Wordle) the available date range. Used both to pre-render the page
    and by the JSON endpoint below, so the calendar needs no follow-up
    requests on load."""
    config = TYPE_FOLDER_CONFIG[type_folder]
    suffix = config['suffix']
    publisher = config['publisher']
    year = int(year)
    month = int(month)
    # Out-of-range values (month 13, year 99999999999, ...) are a client error,
    # not something to run queries / directory scans for.
    if not (1 <= month <= 12 and 1800 <= year <= datetime.now().year + 1):
        raise ValueError("date out of range")
    month_padded = f"{month:02d}"

    progress = {}
    with db() as conn:
        rows = conn.execute('''
            SELECT day_key, ratio, completed, used_check
            FROM progress
            WHERE user_id = ? AND publisher = ? AND year = ? AND month = ?
        ''', (user_id, publisher, str(year), month_padded)).fetchall()

    for day_key, ratio, completed, used_check in rows:
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
    # for the NY Times types (old years didn't publish every day), Spelling Bee,
    # and Connections. Wordle's availability is a contiguous range (wordleRange below).
    available_days = None
    if type_folder == 'spelling-bee':
        available_days = get_spelling_bee_days_in_month(year, month_padded)
    elif type_folder == 'connections':
        available_days = _get_dated_json_days_in_month(CONNECTIONS_DIR, year, month_padded)
    elif type_folder != 'wordle':
        available_days = get_puzzle_days_in_month(publisher, suffix, year, month_padded)

    wordle_range = None
    if type_folder == 'wordle':
        start, end = get_wordle_available_range()
        wordle_range = {
            'startDate': start.isoformat() if start else None,
            'endDate': end.isoformat() if end else None,
        }

    return {
        'type': type_folder,
        'year': year,
        'month': month,
        'progress': progress,
        'availableDays': available_days,
        'range': get_archive_range_info(type_folder),
        'wordleRange': wordle_range,
    }


@app.route('/archive')
@app.route('/archive/<type_folder>')
def archive(type_folder='midi'):
    if 'user_id' not in session:
        return redirect('/login')
    if type_folder not in TYPE_FOLDER_CONFIG:
        type_folder = 'midi'

    # Pre-render the first month the page will show (from ?y=&m=, where m is
    # 0-indexed like JS's Date, or the current month) so it needs no API calls.
    now = datetime.now()
    try:
        y = int(request.args.get('y', ''))
        m0 = int(request.args.get('m', ''))
        if not (1900 <= y <= now.year + 1 and 0 <= m0 <= 11):
            raise ValueError
    except ValueError:
        y, m0 = now.year, now.month - 1

    boot = build_archive_bundle(session['user_id'], type_folder, y, m0 + 1)
    return render_template('archive.html', initial_type=type_folder, boot=boot)


@app.route('/api/archive-progress/<type_folder>/<year>/<month>')
def get_archive_progress(type_folder, year, month):
    if 'user_id' not in session:
        return jsonify({'progress': {}}), 401
    if type_folder not in TYPE_FOLDER_CONFIG:
        return jsonify({'error': 'Invalid puzzle type'}), 400
    try:
        bundle = build_archive_bundle(session['user_id'], type_folder, year, month)
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400
    return jsonify(bundle)


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


@app.route('/api/archive-range/<type_folder>')
def archive_range(type_folder):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if type_folder not in TYPE_FOLDER_CONFIG:
        return jsonify({'error': 'Invalid puzzle type'}), 400
    return jsonify(get_archive_range_info(type_folder))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/login')

    me = get_user_info(user_id)
    if not me:                       # account was deleted
        session.clear()
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

    for p_type, cfg in TYPE_FOLDER_CONFIG.items():
        if p_type == 'wordle':
            today_available[p_type] = get_wordle_word_for_date(today_date) is not None
            today_titles[p_type] = "Guess your way to the correct word."
            continue
        if p_type == 'spelling-bee':
            today_available[p_type] = os.path.exists(os.path.join(
                SPELLING_BEE_DIR, str(today_date.year),
                f"{today_date.month:02d}", f"{today_date.day:02d}.json"))
            today_titles[p_type] = "Make as many words as you can with 7 letters."
            continue
        if p_type == 'connections':
            today_available[p_type] = os.path.exists(os.path.join(
                CONNECTIONS_DIR, str(today_date.year),
                f"{today_date.month:02d}", f"{today_date.day:02d}.json"))
            today_titles[p_type] = "Group words that share a common thread."
            continue

        day_key = day_base + cfg['suffix']
        publisher = cfg['publisher']
        found = False
        title_text = DEFAULT_CROSSWORD_BLURB
        try:
            title_text = f"“{_master_title(publisher, year, month, day_key)}”"
            found = True
        except Exception:
            try:
                fallback = load_fallback_puzzle_json(publisher, year, month, day_key)
            except Exception:
                fallback = None
            if fallback is not None:
                title_text = f"“{fallback.get('title') or DEFAULT_CROSSWORD_BLURB}”"
                found = True

        today_available[p_type] = found
        today_titles[p_type] = "Solve the puzzle in seconds." if p_type == 'mini' else title_text

    # The page needs the user's name/avatar and the last week or so of
    # progress to draw itself; embed them instead of making the browser fetch
    # /api/me and /api/progress after load.
    since = (today_date - timedelta(days=10)).isoformat()
    boot = {'me': me, 'progress': get_progress_summary(user_id, since)}

    return render_template('index.html', today_titles=today_titles,
                           today_available=today_available, boot=boot)


# ---------------------------------------------------------------------------
# Crossword play page
# ---------------------------------------------------------------------------
@app.route('/play/<publisher>/<year>/<month>/<path:day_key>')
def play_puzzle(publisher, year, month, day_key):
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/login')

    # The puzzle and the user's saved progress are embedded in the page so it
    # can start immediately (previously: 2 sequential API calls after load).
    status = 200
    try:
        puzzle, _ = load_puzzle(publisher, year, month, day_key)
        error = None if puzzle is not None else f"No puzzle found for {publisher}/{year}/{month}/{day_key}"
    except Exception:
        app.logger.exception("Failed to load puzzle %s/%s/%s/%s", publisher, year, month, day_key)
        puzzle, error = None, "Could not load that puzzle"

    if puzzle is None:
        boot = {'error': error}
        status = 404
    else:
        boot = {'puzzle': puzzle,
                'saved': get_saved_progress(user_id, publisher, year, month, day_key)}

    return render_template('play.html', publisher=publisher, year=year, month=month,
                           day_key=day_key, boot=boot), status


# ---------------------------------------------------------------------------
# Wordle Routes
# ---------------------------------------------------------------------------
@app.route('/wordle')
def wordle_today():
    if 'user_id' not in session:
        return redirect('/login')
    today = date.today()
    return redirect(f"/wordle/{today.year}/{today.month:02d}/{today.day:02d}")


@app.route('/wordle/<year>/<month>/<day>')
def play_wordle(year, month, day):
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/login')
    try:
        target_date = date(int(year), int(month), int(day))
    except ValueError:
        return "Invalid date", 400

    # Canonical, zero-padded keys: "/wordle/2026/9/5" and "/wordle/2026/09/05"
    # are the same day and must share one saved-progress row.
    year, month_p, day_p = f"{target_date.year:04d}", f"{target_date.month:02d}", f"{target_date.day:02d}"
    word = get_wordle_word_for_date(target_date)
    if word is None:
        return render_template('wordle.html', target_word='', word_unavailable=True,
                               year=year, month=month_p, day=day_p)

    saved = get_saved_progress(user_id, WORDLE_PUBLISHER, year, month_p, day_p)
    return render_template('wordle.html', target_word=word, word_unavailable=False,
                           year=year, month=month_p, day=day_p,
                           max_guesses=WORDLE_MAX_GUESSES, saved=saved)


@app.route('/api/wordle-valid-words')
def wordle_valid_words():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    text = get_valid_wordle_words_text()
    resp = Response(text, mimetype='text/plain')
    # The dictionary basically never changes: let the browser keep it for a
    # week, and answer revalidations with a 304 after that.
    resp.set_etag(_VALID_WORDLE_WORDS_ETAG)
    resp.headers['Cache-Control'] = 'private, max-age=604800'
    return resp.make_conditional(request)


# ---------------------------------------------------------------------------
# Spelling Bee & Connections Routes
# ---------------------------------------------------------------------------
@lru_cache(maxsize=512)
def _read_dated_json_file(path, mtime_ns):
    # mtime is part of the cache key, so a re-scraped puzzle is picked up
    # immediately while stats pages stop re-reading hundreds of files.
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def _load_dated_puzzle(base_dir, target_date: date):
    path = os.path.join(
        base_dir, str(target_date.year),
        f"{target_date.month:02d}", f"{target_date.day:02d}.json"
    )
    try:
        mtime_ns = os.stat(path).st_mtime_ns
        return _read_dated_json_file(path, mtime_ns)
    except (OSError, ValueError):
        return None      # missing, unreadable, or a corrupt/partial JSON file


def get_spelling_bee_puzzle(target_date: date):
    """Loads puzzles/spelling-bee/<year>/<month>/<day>.json for a given date,
    or None if that day doesn't have a (readable) puzzle on disk."""
    data = _load_dated_puzzle(SPELLING_BEE_DIR, target_date)
    if not data or not all(k in data for k in ('center_letter', 'outer_letters', 'answers', 'max_points')):
        return None
    # Callers get their own copy so nobody can mutate the cached one.
    return json.loads(json.dumps(data))


def get_connections_puzzle(target_date: date):
    """Loads puzzles/connections/<year>/<month>/<day>.json for a given date,
    or None if that day doesn't have a valid puzzle on disk."""
    data = _load_dated_puzzle(CONNECTIONS_DIR, target_date)
    if not data:
        return None
    cats = data.get('categories')
    if not isinstance(cats, list) or len(cats) != 4:
        return None
    for c in cats:
        if not isinstance(c, dict) or not c.get('title') or not isinstance(c.get('cards'), list) or len(c['cards']) != 4:
            return None
    return json.loads(json.dumps(data))


@app.route('/spelling-bee')
def spelling_bee_today():
    if 'user_id' not in session:
        return redirect('/login')
    today = date.today()
    return redirect(f"/spelling-bee/{today.year}/{today.month:02d}/{today.day:02d}")


@app.route('/spelling-bee/<year>/<month>/<day>')
def play_spelling_bee(year, month, day):
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/login')
    try:
        target_date = date(int(year), int(month), int(day))
    except ValueError:
        return "Invalid date", 400

    year, month_p, day_p = f"{target_date.year:04d}", f"{target_date.month:02d}", f"{target_date.day:02d}"
    puzzle = get_spelling_bee_puzzle(target_date)
    if puzzle is None:
        return render_template('spelling-bee.html', puzzle_unavailable=True,
                               year=year, month=month_p, day=day_p)

    saved = get_saved_progress(user_id, SPELLING_BEE_PUBLISHER, year, month_p, day_p)
    return render_template('spelling-bee.html', puzzle_unavailable=False, puzzle=puzzle,
                           year=year, month=month_p, day=day_p, saved=saved)


@app.route('/connections')
def connections_today():
    if 'user_id' not in session:
        return redirect('/login')
    today = date.today()
    return redirect(f"/connections/{today.year}/{today.month:02d}/{today.day:02d}")


@app.route('/connections/<year>/<month>/<day>')
def play_connections(year, month, day):
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/login')
    try:
        target_date = date(int(year), int(month), int(day))
    except ValueError:
        return "Invalid date", 400

    year, month_p, day_p = f"{target_date.year:04d}", f"{target_date.month:02d}", f"{target_date.day:02d}"
    puzzle = get_connections_puzzle(target_date)
    if puzzle is None:
        return render_template('connections.html', puzzle_unavailable=True,
                               year=year, month=month_p, day=day_p)

    saved = get_saved_progress(user_id, CONNECTIONS_PUBLISHER, year, month_p, day_p)
    return render_template('connections.html', puzzle_unavailable=False, puzzle=puzzle,
                           year=year, month=month_p, day=day_p, saved=saved)


# ---------------------------------------------------------------------------
# Raw puzzle APIs (no longer used by the bundled pages, kept for other clients)
# ---------------------------------------------------------------------------
@app.route('/api/puzzles-list')
def puzzles_list():
    # These two used to be open to the whole internet (no login), which let
    # anyone download the entire puzzle archive. Like every other route, they
    # now need a logged-in session.
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    resp = jsonify(get_master_index())
    resp.headers['Cache-Control'] = 'private, max-age=3600'
    return resp


@app.route('/api/puzzle-json/<publisher>/<year>/<month>/<path:day_key>')
def get_puzzle_json(publisher, year, month, day_key):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        puzzle, immutable = load_puzzle(publisher, year, month, day_key)
    except Exception:
        app.logger.exception("Failed to load puzzle %s/%s/%s/%s", publisher, year, month, day_key)
        return jsonify({"error": "Could not load that puzzle"}), 500

    if puzzle is None:
        return jsonify({"error": "No puzzle found for that date"}), 404

    resp = jsonify(puzzle)
    if immutable:
        resp.headers['Cache-Control'] = 'private, max-age=86400'
    return resp


def _warm_caches():
    # Load the big master index / word list at startup instead of on the first request.
    for fn in (get_master_index, get_valid_wordle_words_text):
        try:
            fn()
        except Exception as e:
            # Not fatal (they load lazily on first use), but say so in the log
            # instead of failing silently.
            print(f"[startup] could not preload {fn.__name__}: {e}", flush=True)


_warm_caches()


if __name__ == '__main__':
    # Local development only (production runs under gunicorn). The Werkzeug
    # debugger allows arbitrary code execution, so it is opt-in and the server
    # only listens on localhost unless you explicitly say otherwise:
    #   FLASK_DEBUG=1 python app.py          # debugger on
    #   FLASK_HOST=0.0.0.0 python app.py     # reachable from other machines
    app.run(debug=os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes'),
            host=os.environ.get('FLASK_HOST', '127.0.0.1'), port=5235)
