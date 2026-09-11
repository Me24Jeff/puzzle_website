# crossword-app Docker stack

Two folders here, matching your convention:

```
stacks/crossword/        -> goes to /opt/stacks/crossword       (code + compose)
app_data/crossword/      -> goes to /opt/app_data/crossword     (persistent data)
```

## 1. File structure

### `/opt/stacks/crossword/` (the code)

```
crossword/
├── docker-compose.yml       # defines the two services: web + scraper
├── Dockerfile                # one image, used by both services
├── requirements.txt
├── app.py                     # your existing Flask app, unmodified
├── templates/                 # your existing HTML templates, unmodified
│   ├── index.html
│   ├── login.html
│   ├── archive.html
│   ├── play.html
│   ├── wordle.html
│   └── spelling-bee.html
├── scrapers/
│   ├── wordle_scraper.py          # pulls wordle puzzles
│   ├── crossword_scraper.py       # pulls nyt crossword (needs NYT_S_COOKIE)
│   ├── sbsolver_scraper.py        # yours, unmodified - kept as a manual
│   │                               #   backfill/fallback tool
│   └── spelling_bee_scraper.py    # pulls the nyt spelling bee puzzle for today
├── scripts/                       # what cron actually calls
│   ├── crosswords_daily.sh        # daily/mini/midi crosswords
│   ├── wordle_daily.sh            # wordle
│   ├── spelling_bee_daily.sh      # spelling bee
│   └── weekly_xwords_pull.sh      # pulls your GitHub xwords repo
└── docker/
    ├── crontab                    # the schedule
    └── cron-entrypoint.sh         # snapshots env vars, starts cron
```

### `/opt/app_data/crossword/` (the data - bind-mounted into both containers as `/app/data`)

```
crossword/
├── puzzle_progress.db          # sqlite DB - created automatically on first run
├── logs/
│   └── cron.log                 # output of every scrape, tail this to debug
└── puzzles/
    ├── xwords/                  # master archive - pulled weekly from your GitHub repo
    ├── crosswords/              # NY Times/<year>/<month>/<day[-mini|-midi]>.json
    │                             #   one file per scraped daily/mini/midi puzzle
    ├── spelling-bee/            # <year>/<month>/<day>.json, one per day
    ├── wordle_words.txt         # fixed-width file, appended to daily
    └── valid_wordle_words.txt   # you already maintain this; not touched by any scraper
```

Because both containers set `working_dir: /app/data` and mount this folder
there, `app.py`'s existing relative paths (`"puzzles/xwords"`,
`"puzzle_progress.db"`, etc.) resolve correctly with **zero changes to
app.py**. The code lives at `/app` (baked into the image); the data lives at
`/app/data` (bind-mounted from the host).

## 2. Two containers, one image

- **`web`** - runs `gunicorn app:app` on port 5235. This is your site.
- **`scraper`** - same image, but its default command runs `cron` in the
  foreground instead of gunicorn. It fires the three scripts in
  `scripts/` on the schedule in `docker/crontab`, and just sits there
  otherwise. Keeping it separate from `web` means a scraper hiccup can't
  take your site down, and you can `docker compose restart scraper`
  without touching the running site.

## 3. Schedule (times are US/Eastern - see `TZ` in docker-compose.yml)

| When | Script | What |
|---|---|---|
| 22:10 Tuesday - Saturday, 18:10 Saturday - Monday| `crosswords_daily.sh` | NYT Daily/Mini/Midi crosswords (only if `NYT_S_COOKIE` is set)
| 17:10 daily | `wordle_daily.sh` | Wordle |
| 03:10 daily | `spelling_bee_daily.sh` | Spelling Bee |
| 13:00 Sundays | `weekly_xwords_pull.sh` | `git pull`s your archive repo |

## 4. First-time setup

```bash
# 1. Create the folders
sudo mkdir -p /opt/stacks/crossword /opt/app_data/crossword

# 2. Copy the files there (from wherever you extracted this)
sudo cp -r stacks/crossword/. /opt/stacks/crossword/
sudo cp -r app_data/crossword/. /opt/app_data/crossword/

# 3. Build and start
docker compose up -d --build
```

Then visit `http://<your-server>:5235`.

## 5. Useful commands

```bash
# tail scraper logs
tail -f /opt/app_data/crossword/logs/cron.log

# check the cron schedule loaded correctly
docker compose exec scraper crontab -l

# re-run any scraper manually / debug a failure
docker compose exec scraper python3 /app/scrapers/spelling_bee_scraper.py

# rebuild after editing app.py, templates, or scripts
docker compose up -d --build
```
