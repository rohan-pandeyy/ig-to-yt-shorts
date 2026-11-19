"""Dashboard server tests (Flask test client, fake worker). Run: .venv/bin/python -m unittest -v"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ig_to_yt_shorts as yt  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

PASSWORD = 'correct horse'
URL = 'https://www.instagram.com/reel/{}/'


class FakeWorker:
    def __init__(self):
        self.wakes = 0
        self.current_reel_id = None

    def wake(self):
        self.wakes += 1


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store.DB_PATH = os.path.join(self.tmp, 'q.db')
        server.set_password(PASSWORD)
        self.worker = FakeWorker()
        self.app = server.create_app(self.worker)
        self.c = self.app.test_client()

    def login(self, client=None, password=PASSWORD):
        return (client or self.c).post('/login', data={'password': password})

    def post(self, path, body=None, **kw):
        return self.c.post(path, json=body or {}, **kw)

    # ---- auth ----

    def test_everything_requires_login(self):
        self.assertEqual(self.c.get('/api/state').status_code, 401)
        self.assertEqual(self.post('/api/reels', {'urls': URL.format('A')}).status_code, 401)
        r = self.c.get('/')
        self.assertEqual((r.status_code, r.headers['Location']), (302, '/login'))
        self.assertEqual(self.c.get('/login').status_code, 200)
        self.assertEqual(self.c.get('/static/app.js').status_code, 200)

    def test_login_logout_and_wrong_password(self):
        self.assertEqual(self.login(password='nope').status_code, 401)
        r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertIn('HttpOnly', r.headers['Set-Cookie'])
        self.assertIn('SameSite=Lax', r.headers['Set-Cookie'])
        self.assertEqual(self.c.get('/api/state').status_code, 200)
        self.c.post('/logout')
        self.assertEqual(self.c.get('/api/state').status_code, 401)

    def test_lockout_after_repeated_failures(self):
        for _ in range(server.MAX_LOGIN_FAILURES):
            self.login(password='nope')
        r = self.login()                                   # even the right password
        self.assertEqual(r.status_code, 429)

    def test_password_change_signs_out_sessions(self):
        self.login()
        server.set_password('another password')
        app2 = server.create_app(self.worker)              # restart picks up the new key
        c2 = app2.test_client()
        c2.set_cookie('session', self.c.get_cookie('session').value)
        self.assertEqual(c2.get('/api/state').status_code, 401)

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            server.set_password('short')

    def test_cross_origin_and_non_json_refused(self):
        self.login()
        r = self.post('/api/reels', {'urls': URL.format('A')}, headers={'Origin': 'http://evil.example'})
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/api/reels', data={'urls': URL.format('A')})   # form post, not JSON
        self.assertEqual(r.status_code, 415)
        r = self.post('/api/reels', {'urls': URL.format('A')}, headers={'Origin': 'http://localhost'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(store.list_reels()[0]['shortcode'], 'A')

    def test_security_headers(self):
        r = self.c.get('/login')
        self.assertIn("script-src 'self'", r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        # 'no-referrer' makes browsers send "Origin: null" on the login form, which we refuse.
        self.assertEqual(r.headers['Referrer-Policy'], 'same-origin')
        self.assertEqual(self.c.post('/login', data={'password': PASSWORD},
                                     headers={'Origin': 'null'}).status_code, 403)

    # ---- API ----

    def test_add_list_and_hidden_paths(self):
        self.login()
        text = f"{URL.format('A')}\n {URL.format('B')}?igsh=1  \nhttps://example.com/x\n{URL.format('A')}"
        res = self.post('/api/reels', {'urls': text}).get_json()['results']
        self.assertEqual([r['ok'] for r in res], [True, True, False, False])
        self.assertIn('Already in the queue', res[3]['error'])
        store.update_reel(1, video_path='/secret/v.mp4', thumbnail_path='/secret/t.jpg')
        state = self.c.get('/api/state').get_json()
        self.assertEqual([r['shortcode'] for r in state['reels']], ['A', 'B'])
        self.assertNotIn('video_path', state['reels'][0])
        self.assertNotIn('thumbnail_path', state['reels'][0])
        self.assertFalse(state['reels'][0]['has_thumbnail'])
        self.assertEqual(state['schedule']['interval_minutes'], 60)
        self.assertNotIn('password_hash', str(state))
        self.assertNotIn('secret_key', str(state))

    def test_add_validation(self):
        self.login()
        self.assertEqual(self.post('/api/reels', {'urls': '  '}).status_code, 400)
        many = '\n'.join(URL.format(f'R{i}') for i in range(server.MAX_URLS_PER_ADD + 1))
        self.assertEqual(self.post('/api/reels', {'urls': many}).status_code, 400)

    def test_upload_now_retry_done_remove(self):
        self.login()
        self.post('/api/reels', {'urls': f"{URL.format('A')} {URL.format('B')}"})
        r = self.post('/api/reels/2/upload-now', {'title': ' My title '})
        body = r.get_json()
        self.assertEqual((r.status_code, body['reel']['title_override'], body['worker_busy']),
                         (200, 'My title', False))
        self.assertEqual(self.worker.wakes, 1)
        self.worker.current_reel_id = 1
        self.assertTrue(self.post('/api/reels/1/upload-now').get_json()['worker_busy'])

        store.update_reel(2, status='uploaded', youtube_id='vid')
        self.assertEqual(self.post('/api/reels/2/upload-now').status_code, 400)   # never twice
        self.assertEqual(self.c.delete('/api/reels/2').status_code, 400)          # history kept

        store.update_reel(1, status='needs_review')
        self.assertEqual(self.post('/api/reels/1/done').get_json()['reel']['status'], 'uploaded')
        self.post('/api/reels', {'urls': URL.format('C')})
        store.update_reel(3, status='failed', error='x')
        self.assertEqual(self.post('/api/reels/3/retry').get_json()['reel']['status'], 'pending')
        self.assertEqual(self.c.delete('/api/reels/3').status_code, 200)
        self.assertEqual(self.c.delete('/api/reels/99').status_code, 400)

    def test_interval_setting(self):
        self.login()
        r = self.post('/api/settings', {'interval_minutes': 30})
        self.assertEqual(r.get_json()['schedule']['interval_minutes'], 30)
        self.assertEqual(self.worker.wakes, 1)
        self.assertEqual(self.post('/api/settings', {'interval_minutes': 0}).status_code, 400)
        self.assertEqual(self.post('/api/settings', {'interval_minutes': 'abc'}).status_code, 400)

    def test_thumbnail_confined_to_temp(self):
        self.login()
        self.post('/api/reels', {'urls': f"{URL.format('A')} {URL.format('B')}"})
        os.makedirs(yt.TEMP_FOLDER, exist_ok=True)
        thumb = os.path.join(yt.TEMP_FOLDER, 'test-thumb-unittest.jpg')
        with open(thumb, 'wb') as f:
            f.write(b'\xff\xd8jpeg')
        try:
            store.update_reel(1, thumbnail_path=thumb)
            self.assertEqual(self.c.get('/thumb/1').data, b'\xff\xd8jpeg')
            store.update_reel(2, thumbnail_path=os.path.join(yt.SCRIPT_DIR, 'client_secrets.json'))
            self.assertEqual(self.c.get('/thumb/2').status_code, 404)
            store.update_reel(2, thumbnail_path=os.path.join(yt.TEMP_FOLDER, '..', 'token.pickle'))
            self.assertEqual(self.c.get('/thumb/2').status_code, 404)
        finally:
            os.remove(thumb)

    def test_sensitive_files_not_served(self):
        self.login()
        for path in ('/static/../client_secrets.json', '/static/../token.pickle',
                     '/static/../queue.db', '/client_secrets.json', '/token.pickle'):
            self.assertEqual(self.c.get(path).status_code, 404, path)


if __name__ == '__main__':
    unittest.main()
