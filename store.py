"""
store.py
--------
SQLite-backed upload queue and settings for the dashboard.

Queue rules:
- FIFO: reels are processed in the order they were added (by row id). A retried reel
  keeps its original position, so it is never skipped.
- Upload Now puts a reel in a separate priority lane (ordered by when it was requested).
  It does not change the schedule.
- A reel is claimed with a single conditional UPDATE (pending -> downloading), so it can
  never be picked up twice.
- After a crash: reels interrupted before upload go back to pending; reels interrupted
  *during* upload go to needs_review, because YouTube may already have the video.

Statuses: pending -> downloading -> downloaded -> uploading -> uploaded
          failed (retryable), needs_review (check channel, then retry or mark done)
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from ig_to_yt_shorts import SCRIPT_DIR, parse_reel_url

DB_PATH = os.path.join(SCRIPT_DIR, 'queue.db')

PENDING, DOWNLOADING, DOWNLOADED, UPLOADING = 'pending', 'downloading', 'downloaded', 'uploading'
UPLOADED, FAILED, NEEDS_REVIEW = 'uploaded', 'failed', 'needs_review'
IN_PROGRESS = (DOWNLOADING, DOWNLOADED, UPLOADING)
DELETABLE = (PENDING, FAILED, NEEDS_REVIEW)   # uploaded reels stay, so they can't be re-added
UPLOAD_NOW_ALLOWED = (PENDING, FAILED)

DEFAULT_SETTINGS = {
    'interval_minutes': '60',
    'privacy': 'public',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,  -- FIFO order
    shortcode             TEXT NOT NULL UNIQUE,               -- duplicate guard
    url                   TEXT NOT NULL,                      -- canonical URL
    status                TEXT NOT NULL DEFAULT 'pending',
    title                 TEXT,          -- extracted from Instagram
    caption               TEXT,          -- used as the YouTube description
    tags                  TEXT,          -- JSON list
    title_override        TEXT,          -- set via Upload Now
    priority_seq          INTEGER,       -- Upload Now order (NULL = not requested)
    video_path            TEXT,
    thumbnail_path        TEXT,
    youtube_id            TEXT,
    progress              REAL,          -- upload progress 0..1
    error                 TEXT,
    attempts              INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class QueueError(Exception):
    """A user-facing problem with a queue operation (bad URL, duplicate, wrong state)."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d['tags'] = json.loads(d['tags']) if d.get('tags') else []
    return d


def init_db():
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))


# ---------- settings ----------

def get_setting(key):
    with closing(connect()) as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else DEFAULT_SETTINGS.get(key)


def set_setting(key, value):
    with closing(connect()) as conn:
        conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))


def clear_setting(key):
    with closing(connect()) as conn:
        conn.execute('DELETE FROM settings WHERE key = ?', (key,))


# ---------- queue: user actions ----------

def add_reel(url):
    """Append a reel to the end of the queue. Raises QueueError if invalid or already queued."""
    parsed = parse_reel_url(url)
    if not parsed:
        raise QueueError(f"Not an Instagram reel URL: {url}")
    shortcode, canonical = parsed
    ts = now()
    with closing(connect()) as conn:
        try:
            cur = conn.execute(
                'INSERT INTO reels (shortcode, url, created_at, updated_at) VALUES (?, ?, ?, ?)',
                (shortcode, canonical, ts, ts))
        except sqlite3.IntegrityError:
            existing = conn.execute('SELECT status FROM reels WHERE shortcode = ?',
                                    (shortcode,)).fetchone()
            raise QueueError(f"Already in the queue ({existing['status']}): {canonical}")
        return _to_dict(conn.execute('SELECT * FROM reels WHERE id = ?', (cur.lastrowid,)).fetchone())


def list_reels():
    with closing(connect()) as conn:
        return [_to_dict(r) for r in conn.execute('SELECT * FROM reels ORDER BY id')]


def get_reel(reel_id):
    with closing(connect()) as conn:
        return _to_dict(conn.execute('SELECT * FROM reels WHERE id = ?', (reel_id,)).fetchone())


def _transition(reel_id, allowed_from, action, sets, raw_sets=None):
    """Apply `sets` (values) and `raw_sets` (SQL expressions) only if the reel is
    currently in one of `allowed_from`."""
    cols = ', '.join([f'{k} = ?' for k in sets] + [f'{k} = {expr}' for k, expr in (raw_sets or {}).items()])
    marks = ', '.join('?' * len(allowed_from))
    with closing(connect()) as conn:
        row = conn.execute(
            f'UPDATE reels SET {cols}, updated_at = ? WHERE id = ? AND status IN ({marks}) RETURNING *',
            (*sets.values(), now(), reel_id, *allowed_from)).fetchone()
        if row is None:
            current = conn.execute('SELECT status FROM reels WHERE id = ?', (reel_id,)).fetchone()
            if current is None:
                raise QueueError("Reel not found")
            raise QueueError(f"Can't {action} a reel that is {current['status']}")
        return _to_dict(row)


def delete_reel(reel_id):
    marks = ', '.join('?' * len(DELETABLE))
    with closing(connect()) as conn:
        cur = conn.execute(f'DELETE FROM reels WHERE id = ? AND status IN ({marks})',
                           (reel_id, *DELETABLE))
        if cur.rowcount == 0:
            current = conn.execute('SELECT status FROM reels WHERE id = ?', (reel_id,)).fetchone()
            if current is None:
                raise QueueError("Reel not found")
            raise QueueError(f"Can't remove a reel that is {current['status']}")


def request_upload_now(reel_id, title_override=None):
    """Move a pending/failed reel into the priority lane, with an optional custom title."""
    title_override = (title_override or '').strip() or None
    return _transition(reel_id, UPLOAD_NOW_ALLOWED, 'upload now', {
        'status': PENDING, 'title_override': title_override, 'error': None},
        raw_sets={'priority_seq': '(SELECT COALESCE(MAX(priority_seq), 0) + 1 FROM reels)'})


def retry_reel(reel_id):
    """Put a failed (or reviewed) reel back to pending at its original FIFO position.

    It leaves the priority lane but keeps any custom title (only Upload Now changes that).
    """
    return _transition(reel_id, (FAILED, NEEDS_REVIEW), 'retry', {
        'status': PENDING, 'error': None, 'progress': None, 'priority_seq': None})


def mark_done(reel_id):
    """User confirmed a needs_review reel did reach YouTube."""
    return _transition(reel_id, (NEEDS_REVIEW,), 'mark done', {'status': UPLOADED, 'error': None})


# ---------- queue: worker side ----------

def claim_next(priority_only):
    """Atomically take the next reel to process, or None.

    priority_only=True  -> oldest Upload Now request.
    priority_only=False -> oldest pending reel (FIFO by insertion order).
    """
    if priority_only:
        pick = ("SELECT id FROM reels WHERE status = 'pending' AND priority_seq IS NOT NULL "
                "ORDER BY priority_seq LIMIT 1")
    else:
        pick = "SELECT id FROM reels WHERE status = 'pending' ORDER BY id LIMIT 1"
    with closing(connect()) as conn:
        row = conn.execute(
            f"UPDATE reels SET status = 'downloading', attempts = attempts + 1, error = NULL, "
            f"progress = NULL, updated_at = ? WHERE id = ({pick}) AND status = 'pending' RETURNING *",
            (now(),)).fetchone()
    return _to_dict(row)


def update_reel(reel_id, **fields):
    if 'tags' in fields:
        fields['tags'] = json.dumps(fields['tags'])
    cols = ', '.join(f'{k} = ?' for k in fields)
    with closing(connect()) as conn:
        conn.execute(f'UPDATE reels SET {cols}, updated_at = ? WHERE id = ?',
                     (*fields.values(), now(), reel_id))


def has_pending():
    with closing(connect()) as conn:
        return conn.execute("SELECT 1 FROM reels WHERE status = 'pending' LIMIT 1").fetchone() is not None


def recover_interrupted():
    """Run once at startup, before the worker starts. Returns (requeued, needs_review) counts."""
    ts = now()
    with closing(connect()) as conn:
        requeued = conn.execute(
            "UPDATE reels SET status = 'pending', progress = NULL, updated_at = ? "
            "WHERE status IN ('downloading', 'downloaded')", (ts,)).rowcount
        review = conn.execute(
            "UPDATE reels SET status = 'needs_review', updated_at = ?, error = "
            "'Interrupted during upload. Check your YouTube channel, then Retry or Mark done.' "
            "WHERE status = 'uploading'", (ts,)).rowcount
    return requeued, review
