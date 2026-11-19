"""Worker tests with a fake pipeline and a fake clock. Run: .venv/bin/python -m unittest -v"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ig_to_yt_shorts as yt  # noqa: E402
import store  # noqa: E402
import worker  # noqa: E402

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakePipeline:
    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.auth = True
        self.fetch_errors = []    # exceptions raised by successive fetch() calls
        self.upload_errors = []   # exceptions raised by successive upload() calls
        self.fetches = 0
        self.uploads = []         # (url-ish video_path, title, privacy)
        self.auth_checks = 0

    def get_youtube(self):
        if not self.auth:
            raise yt.AuthRequired("sign in needed")
        return object()

    def auth_ok(self):
        self.auth_checks += 1
        return self.auth

    def fetch(self, url):
        self.fetches += 1
        if self.fetch_errors:
            raise self.fetch_errors.pop(0)
        code = yt.parse_reel_url(url)[0]
        path = os.path.join(self.tmpdir, f'{code}.mp4')
        open(path, 'w').close()
        return {'shortcode': code, 'title': f'Video by {code}', 'caption': f'caption {code} #fun',
                'tags': ['fun'], 'video_path': path, 'thumbnail_path': None}

    def upload(self, youtube, video_path, title, tags, privacy, caption, on_progress):
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        on_progress(0.5)
        self.uploads.append((os.path.basename(video_path), title, privacy))
        return f'vid{len(self.uploads)}'


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store.DB_PATH = os.path.join(self.tmp, 'q.db')
        self.clock = Clock(T0)
        self.sleeps = []
        self.pipe = FakePipeline(self.tmp)
        self.w = self.make_worker()
        self.w.prepare()

    def make_worker(self):
        return worker.Worker(self.pipe, clock=self.clock, sleep=self.sleeps.append,
                             lock_path=os.path.join(self.tmp, 'lock'))

    def add(self, *codes):
        return [store.add_reel(f'https://www.instagram.com/reel/{c}/')['id'] for c in codes]

    def next_at(self):
        return worker._parse(store.get_setting('next_scheduled_at'))

    def status(self, rid):
        return store.get_reel(rid)['status']

    def uploaded_files(self):
        return [u[0] for u in self.pipe.uploads]

    # ---- schedule ----

    def test_one_reel_per_interval_fifo(self):
        self.add('A', 'B', 'C')
        self.assertEqual(self.next_at(), T0 + HOUR)          # first run one interval after start
        self.assertFalse(self.w.run_once())                   # not due yet
        self.clock.t = T0 + HOUR
        self.assertTrue(self.w.run_once())
        self.assertFalse(self.w.run_once())                   # only one per run
        self.assertEqual(self.uploaded_files(), ['A.mp4'])
        self.add('D')                                         # appended to the end
        for i in range(2, 5):
            self.clock.t = T0 + i * HOUR
            self.assertTrue(self.w.run_once())
            self.assertFalse(self.w.run_once())
        self.assertEqual(self.uploaded_files(), ['A.mp4', 'B.mp4', 'C.mp4', 'D.mp4'])
        r = store.get_reel(1)
        self.assertEqual((r['status'], r['youtube_id'], r['progress']), ('uploaded', 'vid1', 1.0))
        self.assertEqual(r['caption'], 'caption A #fun')
        self.assertEqual(self.pipe.uploads[0][1:], ('Video by A', 'public'))

    def test_sleeping_through_intervals_uploads_only_one(self):
        self.add('A', 'B', 'C')
        woke = T0 + timedelta(hours=4, minutes=30)            # asleep through 4 runs
        self.clock.t = woke
        self.assertTrue(self.w.run_once())
        self.assertFalse(self.w.run_once())
        self.assertEqual(self.uploaded_files(), ['A.mp4'])
        self.assertEqual(self.next_at(), woke + HOUR)         # one full interval from that run

    def test_schedule_survives_restart(self):
        self.add('A', 'B')
        self.clock.t = T0 + HOUR
        self.w.run_once()
        expected = self.next_at()
        w2 = self.make_worker()                               # "restart"
        w2.prepare()
        self.assertEqual(self.next_at(), expected)
        self.clock.t = expected + timedelta(hours=3)          # PC was off for a while
        self.assertTrue(w2.run_once())
        self.assertFalse(w2.run_once())
        self.assertEqual(self.uploaded_files(), ['A.mp4', 'B.mp4'])

    def test_empty_queue_idles_and_schedule_moves_on(self):
        self.clock.t = T0 + HOUR
        self.assertFalse(self.w.run_once())
        self.assertEqual(self.next_at(), T0 + 2 * HOUR)
        self.add('A')
        self.clock.t = T0 + HOUR + timedelta(minutes=10)
        self.assertFalse(self.w.run_once())                   # waits for the next run
        self.clock.t = T0 + 2 * HOUR
        self.assertTrue(self.w.run_once())

    def test_interval_change(self):
        self.add('A', 'B')
        self.clock.t = T0 + HOUR
        self.w.run_once()                                     # last run at T0+1h
        worker.set_interval(30, now=self.clock.t)
        self.assertEqual(self.next_at(), T0 + HOUR + timedelta(minutes=30))
        worker.set_interval(120, now=self.clock.t)
        self.assertEqual(self.next_at(), T0 + 3 * HOUR)
        with self.assertRaises(ValueError):
            worker.set_interval(0)

    # ---- Upload Now ----

    def test_upload_now_is_independent_of_schedule(self):
        a, b, c = self.add('A', 'B', 'C')
        before = self.next_at()
        store.request_upload_now(c, 'My custom title')
        self.clock.t = T0 + timedelta(minutes=5)
        self.assertTrue(self.w.run_once())
        self.assertFalse(self.w.run_once())                   # no extra scheduled upload
        self.assertEqual(self.pipe.uploads, [('C.mp4', 'My custom title', 'public')])
        self.assertEqual(self.next_at(), before)              # schedule untouched
        self.clock.t = before
        self.assertTrue(self.w.run_once())                    # scheduled run continues FIFO
        self.assertEqual(self.uploaded_files(), ['C.mp4', 'A.mp4'])

    def test_upload_now_blank_title_uses_auto_title(self):
        (a,) = self.add('A')
        store.request_upload_now(a, '   ')
        self.w.run_once()
        self.assertEqual(self.pipe.uploads[0][1], 'Video by A')

    def test_upload_now_requests_run_in_request_order(self):
        a, b = self.add('A', 'B')
        store.request_upload_now(b)
        store.request_upload_now(a)
        self.w.run_once()
        self.w.run_once()
        self.assertEqual(self.uploaded_files(), ['B.mp4', 'A.mp4'])

    # ---- retries and failures ----

    def test_download_retries_are_bounded_with_backoff(self):
        a, b = self.add('A', 'B')
        self.pipe.fetch_errors = [RuntimeError('net down')] * 2
        self.clock.t = T0 + HOUR
        self.w.run_once()
        self.assertEqual(self.status(a), 'uploaded')
        self.assertEqual(self.sleeps, [15, 60])

        self.pipe.fetch_errors = [RuntimeError('reel is private')] * 10
        self.clock.t = T0 + 2 * HOUR
        self.w.run_once()
        self.assertEqual(self.pipe.fetches, 3 + 3)            # exactly FETCH_ATTEMPTS, then stop
        r = store.get_reel(b)
        self.assertEqual(r['status'], 'failed')
        self.assertIn('reel is private', r['error'])
        self.clock.t = T0 + 3 * HOUR
        self.assertFalse(self.w.run_once())                   # failed reel is not picked up again
        self.assertEqual(len(self.pipe.uploads), 1)

    def test_failed_reel_keeps_position_and_custom_title_on_retry(self):
        a, b, c = self.add('A', 'B', 'C')
        store.request_upload_now(b, 'Custom B')
        self.pipe.fetch_errors = [RuntimeError('x')] * 3
        self.w.run_once()
        self.assertEqual(self.status(b), 'failed')
        store.retry_reel(b)
        for i in (1, 2, 3):
            self.clock.t = T0 + i * HOUR
            self.w.run_once()
        self.assertEqual(self.pipe.uploads,
                         [('A.mp4', 'Video by A', 'public'), ('B.mp4', 'Custom B', 'public'),
                          ('C.mp4', 'Video by C', 'public')])

    def test_rejected_upload_fails_without_duplicate_and_reuses_download(self):
        (a,) = self.add('A')
        self.pipe.upload_errors = [yt.UploadNotCreated('bad request', 400, ['invalidTitle'])]
        self.clock.t = T0 + HOUR
        self.w.run_once()
        self.assertEqual(self.status(a), 'failed')
        store.retry_reel(a)
        self.clock.t = T0 + 2 * HOUR
        self.w.run_once()
        self.assertEqual(self.status(a), 'uploaded')
        self.assertEqual(self.pipe.fetches, 1)                # downloaded once, reused
        self.assertEqual(len(self.pipe.uploads), 1)

    def test_unknown_upload_outcome_needs_review_and_is_never_auto_retried(self):
        a, b = self.add('A', 'B')
        self.pipe.upload_errors = [yt.UploadOutcomeUnknown('connection reset')]
        self.clock.t = T0 + HOUR
        self.w.run_once()
        self.assertEqual(self.status(a), 'needs_review')
        self.clock.t = T0 + 2 * HOUR
        self.w.run_once()                                     # moves on to B
        self.assertEqual(self.uploaded_files(), ['B.mp4'])
        self.assertEqual(self.status(a), 'needs_review')
        store.mark_done(a)
        self.assertEqual(self.status(a), 'uploaded')

    def test_crash_during_upload_goes_to_needs_review(self):
        (a,) = self.add('A')
        store.update_reel(a, status='uploading')
        self.make_worker().prepare()
        self.assertEqual(self.status(a), 'needs_review')

    # ---- pauses ----

    def test_quota_exceeded_pauses_and_resumes_with_one_upload(self):
        a, b = self.add('A', 'B')
        self.pipe.upload_errors = [yt.UploadNotCreated('quota', 403, ['quotaExceeded'])]
        self.clock.t = T0 + HOUR
        self.w.run_once()
        self.assertEqual(self.status(a), 'pending')            # never uploaded, so not failed
        self.assertEqual(store.get_setting('pause_reason'), 'quota')
        until = worker._parse(store.get_setting('paused_until'))
        self.assertEqual(until, worker.next_quota_reset(T0 + HOUR))
        store.request_upload_now(b)
        self.clock.t = until - timedelta(minutes=1)
        self.assertFalse(self.w.run_once())                   # nothing while paused
        self.clock.t = until + 5 * HOUR                        # long after reset
        self.assertTrue(self.w.run_once())                    # priority first
        self.assertTrue(self.w.run_once())                    # then the one overdue scheduled run
        self.assertFalse(self.w.run_once())
        self.assertEqual(self.uploaded_files(), ['B.mp4', 'A.mp4'])
        self.assertIsNone(store.get_setting('alert'))

    def test_auth_expired_pauses_without_download_or_busy_loop(self):
        (a,) = self.add('A')
        self.pipe.auth = False
        store.request_upload_now(a)
        self.assertTrue(self.w.run_once())
        self.assertEqual(self.status(a), 'pending')
        self.assertEqual(self.pipe.fetches, 0)
        self.assertIn('sign', store.get_setting('alert'))
        for _ in range(5):                                     # no tight retry loop
            self.assertFalse(self.w.run_once())
        self.assertEqual(self.pipe.auth_checks, 0)             # re-checked only every few minutes
        self.clock.t += worker.AUTH_RECHECK
        self.assertFalse(self.w.run_once())
        self.assertEqual(self.pipe.auth_checks, 1)
        self.pipe.auth = True                                  # user ran --auth
        self.clock.t += worker.AUTH_RECHECK
        self.assertTrue(self.w.run_once())
        self.assertEqual(self.status(a), 'uploaded')


class ThreadTest(unittest.TestCase):
    """The real background thread: wake-up, single-instance lock, clean stop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store.DB_PATH = os.path.join(self.tmp, 'q.db')
        self.lock = os.path.join(self.tmp, 'lock')

    def test_upload_now_wakes_running_thread_and_second_worker_is_refused(self):
        pipe = FakePipeline(self.tmp)
        w = worker.Worker(pipe, poll_seconds=60, lock_path=self.lock)
        w.start()
        try:
            with self.assertRaises(RuntimeError):
                worker.Worker(pipe, lock_path=self.lock).start()
            rid = store.add_reel('https://www.instagram.com/reel/A/')['id']
            store.request_upload_now(rid)
            w.wake()
            deadline = time.time() + 5
            while store.get_reel(rid)['status'] != 'uploaded' and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(store.get_reel(rid)['status'], 'uploaded')
        finally:
            w.stop()
        self.assertFalse(w._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
