"""iPhone Shortcut endpoint and API key tests. Run: .venv/bin/python -m unittest -v"""

import io
import logging
import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import shortcut_key  # noqa: E402
import store  # noqa: E402

PASSWORD = 'correct horse'
ENDPOINT = '/api/shortcut/add'
REEL = 'https://www.instagram.com/reel/{}/'


class FakeWorker:
    def __init__(self):
        self.wakes = 0
        self.current_reel_id = None

    def wake(self):
        self.wakes += 1


class CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(self.format(record))


class ShortcutKeyTest(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = os.path.join(tempfile.mkdtemp(), 'q.db')
        store.init_db()

    def test_format_and_randomness(self):
        key = shortcut_key.generate()
        self.assertRegex(key, r'^rq(-[23456789A-HJ-NP-Z]{4}){8}$')
        self.assertEqual(len({shortcut_key.generate() for _ in range(200)}), 200)

    def test_only_a_hash_is_stored(self):
        key = shortcut_key.create()
        with sqlite3.connect(store.DB_PATH) as conn:
            dump = '\n'.join(conn.iterdump())
        body = key.replace('-', '')[2:]
        self.assertNotIn(key, dump)
        self.assertNotIn(body, dump)
        self.assertRegex(store.get_setting(shortcut_key.HASH_SETTING), r'^[0-9a-f]{64}$')

    def test_verify_regenerate_revoke(self):
        self.assertFalse(shortcut_key.verify('rq-anything'))          # none configured
        key = shortcut_key.create()
        self.assertTrue(shortcut_key.verify(key))
        self.assertTrue(shortcut_key.verify(key.lower().replace('-', ' ')))   # typed loosely
        self.assertFalse(shortcut_key.verify(key[:-1] + ('2' if key[-1] != '2' else '3')))
        self.assertFalse(shortcut_key.verify(''))
        new = shortcut_key.create()
        self.assertFalse(shortcut_key.verify(key))                    # old key dead at once
        self.assertTrue(shortcut_key.verify(new))
        self.assertTrue(shortcut_key.revoke())
        self.assertFalse(shortcut_key.verify(new))
        self.assertFalse(shortcut_key.revoke())


class FindReelLinkTest(unittest.TestCase):
    def ok(self, text, expected):
        self.assertEqual(server.find_reel_link(text), (expected, None), text)

    def bad(self, text, fragment):
        link, reason = server.find_reel_link(text)
        self.assertIsNone(link, text)
        self.assertIn(fragment, reason)

    def test_accepted_shapes(self):
        self.ok('https://www.instagram.com/reel/DAbc123/?igsh=MWZ4dGx0', REEL.format('DAbc123'))
        self.ok('Check this out! https://www.instagram.com/reel/DAbc123/?igsh=x so good',
                REEL.format('DAbc123'))
        self.ok('(https://instagram.com/reel/DAbc123).', REEL.format('DAbc123'))
        self.ok('instagram.com/reels/DAbc123', REEL.format('DAbc123'))
        self.ok('https://m.instagram.com/some.user/reel/DAbc123/', REEL.format('DAbc123'))
        self.ok('https://www.instagram.com/p/DPost99/', 'https://www.instagram.com/p/DPost99/')
        self.ok('first https://www.instagram.com/someone/ then https://www.instagram.com/reel/DAbc123/',
                REEL.format('DAbc123'))

    def test_rejected(self):
        self.bad('', 'No Instagram reel link')
        self.bad('just some words', 'No Instagram reel link')
        self.bad('https://www.youtube.com/shorts/abc', 'No Instagram reel link')
        self.bad('https://evilinstagram.com/reel/DAbc123/', 'No Instagram reel link')
        self.bad('https://instagram.com.evil.example/reel/DAbc123/', 'No Instagram reel link')
        self.bad('https://www.instagram.com/someone/', "isn't a reel or post")
        self.bad('https://www.instagram.com/stories/someone/123/', "isn't a reel or post")
        self.bad('https://www.instagram.com/share/reel/BAabc123/', '/share/ link')
        self.bad('https://www.instagram.com/share/BAabc123/', '/share/ link')
        # parse_reel_url alone would read "share" as a username and the short code as a reel id
        self.bad('Look https://instagram.com/share/reel/BAabc123/?igsh=x', '/share/ link')


class ShortcutEndpointTest(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = os.path.join(tempfile.mkdtemp(), 'q.db')
        server.set_password(PASSWORD)
        self.key = shortcut_key.create()
        self.worker = FakeWorker()
        self.app = server.create_app(self.worker)
        self.c = self.app.test_client()
        self.logs = CaptureLogs()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger().removeHandler(self.logs)
        logging.getLogger().setLevel(logging.WARNING)

    def send(self, url, key=None, client=None, headers=None, **kw):
        h = {'Authorization': f'Bearer {self.key if key is None else key}'}
        h.update(headers or {})
        return (client or self.c).post(ENDPOINT, json={'url': url}, headers=h, **kw)

    def assertReply(self, r, code, status):
        self.assertEqual(r.status_code, code, r.get_json())
        body = r.get_json()
        self.assertEqual(body['status'], status)
        self.assertEqual(body['ok'], status == 'added')
        self.assertTrue(body['message'])
        return body

    # ---- adding ----

    def test_valid_reel_added_like_the_dashboard(self):
        body = self.assertReply(self.send(REEL.format('DAbc123') + '?igsh=xyz'), 201, 'added')
        self.assertEqual((body['url'], body['shortcode'], body['position']),
                         (REEL.format('DAbc123'), 'DAbc123', 1))
        self.assertIn('#1 in line', body['message'])
        reel = store.list_reels()[0]
        self.assertEqual((reel['status'], reel['priority_seq']), ('pending', None))
        self.assertEqual(self.worker.wakes, 0)                       # never starts an upload

    def test_surrounding_text(self):
        r = self.send('Watch this 😂 https://www.instagram.com/reel/DText01/?igsh=abc via Instagram')
        self.assertEqual(self.assertReply(r, 201, 'added')['shortcode'], 'DText01')

    def test_fifo_order_preserved(self):
        store.add_reel(REEL.format('First'))
        store.request_upload_now(store.add_reel(REEL.format('Prio'))['id'], '')
        body = self.assertReply(self.send(REEL.format('Third')), 201, 'added')
        self.assertEqual(body['position'], 3)
        store.add_reel(REEL.format('Fourth'))
        fifo = [r['shortcode'] for r in store.list_reels() if r['priority_seq'] is None]
        self.assertEqual(fifo, ['First', 'Third', 'Fourth'])

    def test_duplicates(self):
        self.send(REEL.format('DDup001'))
        body = self.assertReply(self.send(REEL.format('DDup001') + '?igsh=other'), 200, 'duplicate')
        self.assertEqual(body['message'], 'Already in the queue.')
        reel = store.add_reel(REEL.format('DDone01'))
        store.update_reel(reel['id'], status='uploaded')
        self.assertEqual(self.assertReply(self.send(REEL.format('DDone01')), 200, 'duplicate')['message'],
                         'Already uploaded to YouTube.')
        self.assertEqual(len(store.list_reels()), 2)

    def test_invalid_links(self):
        for text in ('https://www.instagram.com/someone/', 'https://evilinstagram.com/reel/DAbc123/',
                     'hello', 'https://www.instagram.com/share/reel/BAabc/'):
            self.assertReply(self.send(text), 400, 'invalid')
        self.assertReply(self.c.post(ENDPOINT, json={'url': 42},
                                     headers={'Authorization': f'Bearer {self.key}'}), 400, 'invalid')
        self.assertReply(self.c.post(ENDPOINT, json=['x'],
                                     headers={'Authorization': f'Bearer {self.key}'}), 400, 'invalid')
        self.assertEqual(store.list_reels(), [])

    def test_unexpected_error_is_generic(self):
        original = store.add_reel
        store.add_reel = lambda url: (_ for _ in ()).throw(RuntimeError('disk on fire /secret/path'))
        try:
            body = self.assertReply(self.send(REEL.format('DBoom01')), 500, 'error')
        finally:
            store.add_reel = original
        self.assertNotIn('disk on fire', body['message'])

    # ---- authentication ----

    def test_missing_and_wrong_keys(self):
        r = self.c.post(ENDPOINT, json={'url': REEL.format('DAbc123')})
        self.assertReply(r, 401, 'unauthorized')
        for header in ('Basic ' + self.key, 'Bearer', 'Bearer ', self.key,
                       'Bearer rq-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA'):
            c = self.app.test_client()       # separate addresses, so the lockout doesn't kick in
            r = c.post(ENDPOINT, json={'url': REEL.format('DAbc123')}, headers={'Authorization': header},
                       environ_base={'REMOTE_ADDR': f'10.0.0.{len(header)}'})
            self.assertReply(r, 401, 'unauthorized')
        self.assertEqual(store.list_reels(), [])

    def test_key_in_query_string_is_ignored(self):
        r = self.c.post(f'{ENDPOINT}?key={self.key}&access_token={self.key}',
                        json={'url': REEL.format('DAbc123')})
        self.assertReply(r, 401, 'unauthorized')

    def test_revoked_and_unconfigured(self):
        shortcut_key.revoke()
        self.assertReply(self.send(REEL.format('DAbc123')), 401, 'not_configured')
        shortcut_key.create()
        self.assertReply(self.send(REEL.format('DAbc123'), client=self.app.test_client(),
                                   environ_base={'REMOTE_ADDR': '10.9.9.9'}), 401, 'unauthorized')

    def test_dashboard_session_does_not_work_here(self):
        self.c.post('/login', data={'password': PASSWORD})
        self.assertEqual(self.c.get('/api/state').status_code, 200)
        r = self.c.post(ENDPOINT, json={'url': REEL.format('DAbc123')})
        self.assertReply(r, 401, 'unauthorized')

    def test_cross_origin_refused(self):
        r = self.send(REEL.format('DAbc123'), headers={'Origin': 'http://evil.example'})
        self.assertReply(r, 403, 'forbidden')
        self.assertEqual(store.list_reels(), [])

    def test_key_gives_access_to_nothing_else(self):
        reel = store.add_reel(REEL.format('DOther1'))
        auth = {'Authorization': f'Bearer {self.key}'}
        checked = 0
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint in ('shortcut_add', 'static', 'login'):
                continue
            path = rule.rule.replace('<int:reel_id>', str(reel['id']))
            for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
                r = self.c.open(path, method=method, headers=auth, json={'interval_minutes': 15})
                if path.startswith('/api/'):
                    self.assertEqual(r.status_code, 401, f'{method} {path}')
                else:
                    self.assertIn(r.status_code, (302, 401), f'{method} {path}')
                    if r.status_code == 302:
                        self.assertTrue(r.headers['Location'].endswith('/login'), f'{method} {path}')
                checked += 1
        self.assertGreaterEqual(checked, 9)
        self.assertEqual(store.get_reel(reel['id'])['status'], 'pending')   # nothing changed
        self.assertEqual(store.get_setting('interval_minutes'), '60')
        self.assertEqual(self.worker.wakes, 0)
        # and it can't be used as a dashboard password
        self.assertEqual(self.c.post('/login', data={'password': self.key}).status_code, 401)

    # ---- lockout ----

    def test_lockout_uses_the_real_address_not_forwarded_headers(self):
        for i in range(server.MAX_KEY_FAILURES):
            r = self.send(REEL.format('DAbc123'), key='rq-wrong',
                          headers={'X-Forwarded-For': f'203.0.113.{i}', 'X-Real-IP': f'198.51.100.{i}'})
            self.assertReply(r, 401, 'unauthorized')
        r = self.send(REEL.format('DAbc123'), headers={'X-Forwarded-For': '192.0.2.77'})
        self.assertReply(r, 429, 'locked')                     # even the right key, while locked
        other = self.app.test_client()
        r = self.send(REEL.format('DAbc123'), client=other, environ_base={'REMOTE_ADDR': '192.168.1.50'})
        self.assertReply(r, 201, 'added')                      # another device is unaffected

    def test_success_resets_failure_count(self):
        for _ in range(server.MAX_KEY_FAILURES - 1):
            self.send(REEL.format('DAbc123'), key='rq-wrong')
        self.assertReply(self.send(REEL.format('DReset1')), 201, 'added')
        for _ in range(server.MAX_KEY_FAILURES - 1):
            self.send(REEL.format('DAbc123'), key='rq-wrong')
        self.assertReply(self.send(REEL.format('DReset2')), 201, 'added')

    # ---- the key never leaks ----

    def test_key_never_in_responses_or_logs(self):
        bodies = [
            self.send(REEL.format('DLeak01')),
            self.send(REEL.format('DLeak01')),
            self.send('not a link'),
            self.send(REEL.format('DLeak02'), key=self.key + 'X'),
            self.send(REEL.format('DLeak03'), headers={'Origin': 'http://evil.example'}),
        ]
        normalized = self.key.replace('-', '')
        for r in bodies:
            text = r.get_data(as_text=True) + str(dict(r.headers))
            self.assertNotIn(self.key, text)
            self.assertNotIn(normalized, text.replace('-', ''))
        logged = '\n'.join(self.logs.text)
        self.assertIn('Shortcut added reel DLeak01', logged)
        self.assertIn('wrong key', logged)
        self.assertNotIn(self.key, logged)
        self.assertNotIn(normalized[2:12], logged.replace('-', ''))
        self.assertNotIn('Bearer', logged)
        state = self.c.post('/login', data={'password': PASSWORD}) and self.c.get('/api/state')
        self.assertNotIn(normalized[2:12], state.get_data(as_text=True).replace('-', ''))


class NewKeyCommandTest(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = os.path.join(tempfile.mkdtemp(), 'q.db')
        store.init_db()

    def test_refuses_to_print_into_a_file_or_pipe(self):
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            self.assertEqual(server.new_shortcut_key(8080, out=io.StringIO()), 1)
        finally:
            sys.stderr = old
        self.assertFalse(shortcut_key.is_configured())
        self.assertIn('terminal', err.getvalue())

    def test_prints_key_once_with_a_key_only_qr(self):
        class Tty(io.StringIO):
            encoding = 'utf-8'

            def isatty(self):
                return True
        out = Tty()
        self.assertEqual(server.new_shortcut_key(8080, out=out), 0)
        text = out.getvalue()
        key = re.search(r'rq(-[0-9A-Z]{4}){8}', text).group(0)
        self.assertTrue(shortcut_key.verify(key))
        self.assertIn('/api/shortcut/add', text)
        self.assertIn('Bearer ' + key, text)
        # the QR encodes exactly the key, nothing else
        import startup
        self.assertIn(startup.render_qr(key, 'ansi' if os.name != 'nt' else 'blocks'), text)
        self.assertNotIn('previous key', text)
        out2 = Tty()
        server.new_shortcut_key(8080, out=out2)
        self.assertIn('previous key stopped working', out2.getvalue())
        self.assertFalse(shortcut_key.verify(key))


if __name__ == '__main__':
    unittest.main()
