"""
worker.py
---------
The background worker: a single thread that processes the upload queue.

Schedule
- `next_scheduled_at` is stored in the database, so it survives restarts.
- When it is due, exactly ONE reel (the oldest pending, FIFO) is processed, and
  `next_scheduled_at` becomes (start of that run + interval). Missed intervals while the
  PC was asleep or off never stack up: at most one overdue run happens.
- If the queue is empty at a due time, nothing is uploaded and the schedule moves on.
- Upload Now (priority lane) runs as soon as the worker is free and never changes the
  schedule. Only one reel is ever processed at a time.
- Pauses: if YouTube's daily quota is exhausted, or the sign-in has expired, nothing runs.
  The reel goes back to pending (it was never uploaded). A scheduled run that was due
  stays due, and runs once when the pause clears.

Retries (all bounded)
- Download: FETCH_ATTEMPTS tries with FETCH_BACKOFF_SECONDS between them, then failed.
- Upload: resumed within the same upload session (ig_to_yt_shorts.UPLOAD_RETRIES), which
  can never create a second video. If the outcome is unknown, the reel goes to
  needs_review and is never retried automatically.
- Failed reels stay in the list until you retry or remove them.

Run `python worker.py --help` for a small CLI to test the worker without the dashboard.
"""

import argparse
import fcntl
import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ig_to_yt_shorts as yt
import store

log = logging.getLogger('worker')

POLL_SECONDS = 30                 # how often the idle worker re-checks the clock and queue
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = (15, 60)  # wait after the 1st and 2nd failed download
AUTH_RECHECK = timedelta(minutes=5)
MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES = 1, 24 * 60
QUOTA_REASONS = {'quotaExceeded', 'uploadLimitExceeded', 'dailyLimitExceeded'}
PACIFIC = ZoneInfo('America/Los_Angeles')   # YouTube API quota resets at midnight Pacific
LOCK_PATH = os.path.join(yt.SCRIPT_DIR, '.worker.lock')


def utcnow():
    return datetime.now(timezone.utc)


def _parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def _fmt(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def next_quota_reset(now):
    """A few minutes after the next midnight, Pacific time."""
    pt = now.astimezone(PACIFIC)
    reset = (pt + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return reset.astimezone(timezone.utc)


def get_interval_minutes():
    try:
        value = int(store.get_setting('interval_minutes'))
    except (TypeError, ValueError):
        value = 60
    return min(max(value, MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)


def set_interval(minutes, now=None):
    """Change the interval. The next run becomes last scheduled run + new interval
    (or now + interval if there hasn't been one). If that is already past, it runs next poll."""
    minutes = int(minutes)
    if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
        raise ValueError(f"Interval must be {MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES} minutes")
    now = now or utcnow()
    store.set_setting('interval_minutes', minutes)
    base = _parse(store.get_setting('last_scheduled_run_at')) or now
    store.set_setting('next_scheduled_at', _fmt(base + timedelta(minutes=minutes)))


class Pipeline:
    """The real processing engine (ig_to_yt_shorts). Tests swap in a fake."""

    def __init__(self):
        self._ffmpeg_checked = False
        self._ffmpeg_path = None

    def get_youtube(self):
        return yt.get_authenticated_service(interactive=False)

    def auth_ok(self):
        try:
            self.get_youtube()
            return True
        except yt.AuthRequired:
            return False

    def fetch(self, url):
        if not self._ffmpeg_checked:
            self._ffmpeg_path = yt.ensure_ffmpeg()
            self._ffmpeg_checked = True
        return yt.fetch_reel(url, self._ffmpeg_path, quiet=True)

    def upload(self, youtube, video_path, title, description, tags, privacy, on_progress):
        response = yt.initialize_upload(youtube, video_path, title, description,
                                        yt.with_shorts_tag(tags), privacy, on_progress)
        return response['id']


def _token_marker():
    try:
        return os.path.getmtime(yt.TOKEN_FILE)
    except OSError:
        return None


class Worker:
    def __init__(self, pipeline=None, clock=utcnow, sleep=None, poll_seconds=POLL_SECONDS,
                 lock_path=LOCK_PATH):
        self.pipeline = pipeline or Pipeline()
        self.clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.sleep = sleep or self._stop.wait   # interruptible by stop()
        self.poll_seconds = poll_seconds
        self.lock_path = lock_path
        self._lock_file = None
        self._thread = None
        self._auth_checked_at = None
        self._auth_marker = None
        self.current_reel_id = None

    # ---------- lifecycle ----------

    def prepare(self):
        """Create tables, recover from a crash, make sure a schedule exists."""
        store.init_db()
        requeued, review = store.recover_interrupted()
        if requeued or review:
            log.warning("Recovered after restart: %d requeued, %d need review", requeued, review)
        if not store.get_setting('next_scheduled_at'):
            first = self.clock() + timedelta(minutes=get_interval_minutes())
            store.set_setting('next_scheduled_at', _fmt(first))

    def start(self, background=True):
        self._acquire_lock()
        self.prepare()
        log.info("Worker started. Next scheduled run: %s", store.get_setting('next_scheduled_at'))
        if background:
            self._thread = threading.Thread(target=self._loop, name='upload-worker', daemon=True)
            self._thread.start()
        else:
            self._loop()

    def stop(self, timeout=10):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        if self._lock_file:
            self._lock_file.close()   # releases the single-instance lock
            self._lock_file = None

    def wake(self):
        """Call after Upload Now, adding reels or changing the interval, to skip the poll wait."""
        self._wake.set()

    def _acquire_lock(self):
        # One worker per machine; a second one would double the schedule.
        self._lock_file = open(self.lock_path, 'w')
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("Another worker is already running (dashboard or worker.py run)")

    def _loop(self):
        while not self._stop.is_set():
            try:
                processed = self.run_once()
            except Exception:
                log.exception("Unexpected worker error")
                processed = False
            if not processed:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    # ---------- one step ----------

    def run_once(self):
        """Do at most one reel of work. Returns True if a reel was processed."""
        now = self.clock()
        if self._is_paused(now):
            return False

        reel = store.claim_next(priority_only=True)
        if reel:
            log.info("Upload Now: reel #%d", reel['id'])
            self._process(reel)
            return True

        due = _parse(store.get_setting('next_scheduled_at'))
        if due is None or now < due:
            return False
        # Schedule from the start of this run, before processing, so a crash mid-run
        # can't cause an extra scheduled upload.
        store.set_setting('last_scheduled_run_at', _fmt(now))
        store.set_setting('next_scheduled_at', _fmt(now + timedelta(minutes=get_interval_minutes())))
        reel = store.claim_next(priority_only=False)
        if reel is None:
            log.info("Scheduled run: queue empty")
            return False
        log.info("Scheduled run: reel #%d", reel['id'])
        if self._process(reel) == 'paused':
            store.set_setting('next_scheduled_at', _fmt(now))  # still due once the pause clears
        return True

    # ---------- pause handling ----------

    def _pause(self, reason, message, until=None):
        store.set_setting('pause_reason', reason)
        store.set_setting('alert', message)
        if until:
            store.set_setting('paused_until', _fmt(until))
        else:
            store.clear_setting('paused_until')
        self._auth_checked_at = self.clock()
        self._auth_marker = _token_marker()
        log.warning("Paused (%s): %s", reason, message)

    def _resume(self):
        for key in ('pause_reason', 'paused_until', 'alert'):
            store.clear_setting(key)
        log.info("Pause cleared, resuming")

    def _is_paused(self, now):
        reason = store.get_setting('pause_reason')
        if not reason:
            return False
        if reason == 'quota':
            until = _parse(store.get_setting('paused_until'))
            if until is None or now >= until:
                self._resume()
                return False
            return True
        # Sign-in: re-check when the token file changes (you ran --auth) or every few minutes.
        marker = _token_marker()
        if marker != self._auth_marker or self._auth_checked_at is None \
                or now - self._auth_checked_at >= AUTH_RECHECK:
            self._auth_checked_at, self._auth_marker = now, marker
            if self.pipeline.auth_ok():
                self._resume()
                return False
        return True

    # ---------- the pipeline for one reel ----------

    def _process(self, reel):
        self.current_reel_id = reel['id']
        try:
            return self._run_pipeline(reel)
        finally:
            self.current_reel_id = None

    def _run_pipeline(self, reel):
        rid = reel['id']

        # 1. Sign-in first, so an expired sign-in doesn't waste a download.
        try:
            youtube = self.pipeline.get_youtube()
        except yt.AuthRequired as e:
            store.update_reel(rid, status=store.PENDING)
            self._pause('auth', str(e))
            return 'paused'
        except Exception as e:
            store.update_reel(rid, status=store.FAILED, error=f"Couldn't connect to YouTube: {e}")
            return 'failed'

        # 2. Download, unless a previous attempt already did.
        if not (reel.get('video_path') and os.path.exists(reel['video_path'])):
            fetched = self._fetch_with_retries(reel)
            if fetched is None:
                return 'failed'
            auto_title, _ = yt.build_youtube_metadata(
                fetched['caption'], fetched['handle'], fetched['full_name'], reel['url'])
            store.update_reel(rid, status=store.DOWNLOADED, title=auto_title,
                              caption=fetched['caption'], tags=fetched['tags'],
                              uploader_handle=fetched['handle'], uploader_name=fetched['full_name'],
                              video_path=fetched['video_path'], thumbnail_path=fetched['thumbnail_path'])
            reel = store.get_reel(rid)

        # 3. Title & description come only from stored data (caption, author, custom title),
        #    so a retry always produces exactly the same title.
        try:
            title, description = yt.build_youtube_metadata(
                reel.get('caption'), reel.get('uploader_handle'), reel.get('uploader_name'),
                reel['url'], custom_title=reel.get('title_override'))
        except ValueError as e:
            store.update_reel(rid, status=store.FAILED, error=str(e))
            return 'failed'

        # 4. Upload. Once status is 'uploading', a crash means needs_review, never a retry.
        store.update_reel(rid, status=store.UPLOADING, progress=0.0)
        try:
            video_id = self.pipeline.upload(
                youtube, reel['video_path'], title, description, reel.get('tags') or [],
                store.get_setting('privacy'), lambda p: store.update_reel(rid, progress=p))
        except yt.UploadNotCreated as e:
            if QUOTA_REASONS & set(e.reasons):
                store.update_reel(rid, status=store.PENDING, progress=None)
                until = next_quota_reset(self.clock())
                self._pause('quota', f"YouTube upload quota used up; resuming after {_fmt(until)}", until)
                return 'paused'
            if e.status == 401:
                store.update_reel(rid, status=store.PENDING, progress=None)
                self._pause('auth', "YouTube rejected the sign-in. On the PC, run: "
                                    ".venv/bin/python ig_to_yt_shorts.py --auth")
                return 'paused'
            store.update_reel(rid, status=store.FAILED, progress=None, error=str(e))
            return 'failed'
        except Exception as e:  # UploadOutcomeUnknown, or anything unexpected mid-upload
            store.update_reel(rid, status=store.NEEDS_REVIEW, error=(
                f"{e}. The video may or may not be on YouTube: check your channel, "
                "then Retry or Mark done."))
            return 'needs_review'

        store.update_reel(rid, status=store.UPLOADED, youtube_id=video_id, progress=1.0, error=None)
        log.info("Reel #%d uploaded: https://youtube.com/shorts/%s", rid, video_id)
        return 'uploaded'

    def _fetch_with_retries(self, reel):
        last_error = None
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                return self.pipeline.fetch(reel['url'])
            except Exception as e:
                last_error = e
                log.warning("Download attempt %d/%d failed for reel #%d: %s",
                            attempt, FETCH_ATTEMPTS, reel['id'], e)
                if attempt < FETCH_ATTEMPTS:
                    self.sleep(FETCH_BACKOFF_SECONDS[attempt - 1])
                    if self._stop.is_set():
                        break
        store.update_reel(reel['id'], status=store.FAILED,
                          error=f"Download failed after {attempt} attempt(s): {last_error}")
        return None


def status():
    """Snapshot of the worker's schedule and pause state (from the database)."""
    return {
        'interval_minutes': get_interval_minutes(),
        'next_scheduled_at': store.get_setting('next_scheduled_at'),
        'last_scheduled_run_at': store.get_setting('last_scheduled_run_at'),
        'pause_reason': store.get_setting('pause_reason'),
        'paused_until': store.get_setting('paused_until'),
        'alert': store.get_setting('alert'),
        'privacy': store.get_setting('privacy'),
    }


# ---------- CLI for testing the worker without the dashboard ----------

def _cli():
    p = argparse.ArgumentParser(description="Upload queue worker (test CLI)")
    sub = p.add_subparsers(dest='cmd', required=True)
    run = sub.add_parser('run', help='Run the worker in the foreground (Ctrl+C to stop)')
    run.add_argument('--poll', type=int, default=POLL_SECONDS, help='Idle poll seconds')
    sub.add_parser('list', help='Show the queue')
    sub.add_parser('status', help='Show schedule and pause state')
    add = sub.add_parser('add', help='Append reel URLs to the queue')
    add.add_argument('urls', nargs='+')
    now = sub.add_parser('now', help='Upload Now (picked up by a running worker within one poll)')
    now.add_argument('id', type=int)
    now.add_argument('--title', default=None, help='Custom YouTube title')
    for name, text in (('retry', 'Retry a failed / needs_review reel'),
                       ('done', 'Mark a needs_review reel as uploaded'),
                       ('remove', 'Remove a pending / failed / needs_review reel')):
        sub.add_parser(name, help=text).add_argument('id', type=int)
    iv = sub.add_parser('interval', help='Set the schedule interval in minutes')
    iv.add_argument('minutes', type=int)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store.init_db()
    try:
        if args.cmd == 'run':
            try:
                Worker(poll_seconds=args.poll).start(background=False)
            except KeyboardInterrupt:
                print("\nStopped.")
        elif args.cmd == 'list':
            for r in store.list_reels():
                title = r['title_override'] and f"{r['title_override']!r} (custom)" or (r['title'] or '')
                extra = (f"https://youtube.com/shorts/{r['youtube_id']}" if r['youtube_id']
                         else (r['error'] or ''))
                prio = ' [upload now]' if r['priority_seq'] and r['status'] == 'pending' else ''
                print(f"#{r['id']:<4} {r['status']:<13}{prio} {r['url']}  {title}  {extra}".rstrip())
        elif args.cmd == 'status':
            for k, v in status().items():
                print(f"{k:22} {v}")
        elif args.cmd == 'add':
            for url in args.urls:
                try:
                    print(f"added #{store.add_reel(url)['id']}: {url}")
                except store.QueueError as e:
                    print(f"skipped: {e}")
        elif args.cmd == 'now':
            store.request_upload_now(args.id, args.title)
            print(f"#{args.id} queued for Upload Now")
        elif args.cmd == 'retry':
            store.retry_reel(args.id)
            print(f"#{args.id} back to pending")
        elif args.cmd == 'done':
            store.mark_done(args.id)
            print(f"#{args.id} marked uploaded")
        elif args.cmd == 'remove':
            store.delete_reel(args.id)
            print(f"#{args.id} removed")
        elif args.cmd == 'interval':
            set_interval(args.minutes)
            print(f"Interval {args.minutes} min; next run {store.get_setting('next_scheduled_at')}")
    except (store.QueueError, ValueError, RuntimeError) as e:
        sys.exit(str(e))


if __name__ == '__main__':
    _cli()
