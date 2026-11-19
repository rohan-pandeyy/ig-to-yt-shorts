"""
server.py
---------
The dashboard: a Flask app and the background upload worker in one process.

    .venv/bin/python server.py --set-password   # once, on the PC
    .venv/bin/python server.py                  # then open http://<this PC's IP>:8080

Security
- Every page and API call needs a login (password hash + random session key stored in
  queue.db). Five wrong passwords from one address lock it out for five minutes.
- State-changing requests must be JSON (or the login form) and come from this origin.
- Only static/ and reel thumbnails (looked up by id, confined to temp/) are served;
  credentials, token, database and videos never are. Flask debug mode is never enabled.
"""

import argparse
import getpass
import logging
import os
import re
import secrets
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import (Flask, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import ig_to_yt_shorts as yt
import store
import worker as worker_mod

STATIC_DIR = os.path.join(yt.SCRIPT_DIR, 'static')
TEMPLATE_DIR = os.path.join(yt.SCRIPT_DIR, 'templates')
DEFAULT_PORT = 8080
MIN_PASSWORD_LEN = 8
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 300
MAX_URLS_PER_ADD = 50
# Fields the browser gets; file paths stay on the server.
PUBLIC_FIELDS = ('id', 'shortcode', 'url', 'status', 'title', 'caption', 'title_override',
                 'priority_seq', 'youtube_id', 'progress', 'error', 'attempts',
                 'created_at', 'updated_at')

log = logging.getLogger('server')


def _public(reel):
    data = {k: reel.get(k) for k in PUBLIC_FIELDS}
    data['has_thumbnail'] = bool(reel.get('thumbnail_path')) and os.path.exists(reel['thumbnail_path'])
    return data


def set_password(password):
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    store.init_db()
    store.set_setting('password_hash', generate_password_hash(password))
    store.set_setting('secret_key', secrets.token_hex(32))   # logs out every existing session


def create_app(the_worker):
    """the_worker needs .wake() and .current_reel_id (a real Worker, or a fake in tests)."""
    store.init_db()
    secret = store.get_setting('secret_key')
    if not secret:
        secret = secrets.token_hex(32)
        store.set_setting('secret_key', secret)

    app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATE_DIR)
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    login_failures = {}   # ip -> (count, locked_until)

    # ---------- request guards ----------

    @app.before_request
    def guard():
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('Origin')
            if origin and urlparse(origin).netloc != request.host:
                return jsonify(error="Cross-origin request refused"), 403
            if request.method == 'POST' and request.path.startswith('/api/') and not request.is_json:
                return jsonify(error="Expected JSON"), 415
        if request.endpoint in ('login', 'static') or session.get('auth'):
            return None
        if request.path.startswith('/api/'):
            return jsonify(error="Not logged in"), 401
        return redirect(url_for('login'))

    @app.after_request
    def security_headers(resp):
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        # Not 'no-referrer': with that, browsers send "Origin: null" on form posts and
        # the origin check above would refuse the login form.
        resp.headers['Referrer-Policy'] = 'same-origin'
        if request.path.startswith('/api/'):
            resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.errorhandler(store.QueueError)
    @app.errorhandler(ValueError)
    def bad_request(e):
        return jsonify(error=str(e)), 400

    # ---------- login ----------

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET':
            return render_template('login.html', error=None)
        ip = request.remote_addr
        count, locked_until = login_failures.get(ip, (0, 0))
        if locked_until > time.time():
            return render_template('login.html', error="Too many attempts. Try again in a few minutes."), 429
        password_hash = store.get_setting('password_hash')
        if password_hash and check_password_hash(password_hash, request.form.get('password', '')):
            login_failures.pop(ip, None)
            session.clear()
            session['auth'] = True
            session.permanent = True
            return redirect(url_for('index'))
        count += 1
        login_failures[ip] = (count, time.time() + LOCKOUT_SECONDS if count >= MAX_LOGIN_FAILURES else 0)
        log.warning("Failed login from %s (%d)", ip, count)
        return render_template('login.html', error="Wrong password."), 401

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    # ---------- pages ----------

    @app.get('/')
    def index():
        return send_from_directory(STATIC_DIR, 'index.html')

    @app.get('/thumb/<int:reel_id>')
    def thumbnail(reel_id):
        reel = store.get_reel(reel_id)
        path = reel and reel.get('thumbnail_path')
        if not path:
            return '', 404
        real = os.path.realpath(path)
        if os.path.commonpath([real, os.path.realpath(yt.TEMP_FOLDER)]) != os.path.realpath(yt.TEMP_FOLDER) \
                or not os.path.isfile(real):
            return '', 404
        return send_file(real, max_age=86400)

    # ---------- API ----------

    @app.get('/api/state')
    def state():
        return jsonify(
            reels=[_public(r) for r in store.list_reels()],
            schedule=worker_mod.status(),
            current_reel_id=the_worker.current_reel_id,
            server_time=datetime.now(timezone.utc).isoformat(timespec='seconds'),
            interval_limits=[worker_mod.MIN_INTERVAL_MINUTES, worker_mod.MAX_INTERVAL_MINUTES],
        )

    @app.post('/api/reels')
    def add_reels():
        raw = (request.get_json() or {}).get('urls', '')
        urls = raw if isinstance(raw, list) else re.split(r'\s+', str(raw))
        urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        if not urls:
            raise ValueError("Paste at least one reel link")
        if len(urls) > MAX_URLS_PER_ADD:
            raise ValueError(f"At most {MAX_URLS_PER_ADD} links at a time")
        results = []
        for url in urls:
            try:
                results.append({'url': url, 'ok': True, 'id': store.add_reel(url)['id']})
            except store.QueueError as e:
                results.append({'url': url, 'ok': False, 'error': str(e)})
        return jsonify(results=results)

    @app.delete('/api/reels/<int:reel_id>')
    def remove_reel(reel_id):
        store.delete_reel(reel_id)
        return jsonify(ok=True)

    @app.post('/api/reels/<int:reel_id>/upload-now')
    def upload_now(reel_id):
        title = (request.get_json() or {}).get('title') or ''
        if not isinstance(title, str):
            raise ValueError("Title must be text")
        reel = store.request_upload_now(reel_id, title)
        busy = the_worker.current_reel_id is not None
        the_worker.wake()
        return jsonify(reel=_public(reel), worker_busy=busy)

    @app.post('/api/reels/<int:reel_id>/retry')
    def retry(reel_id):
        return jsonify(reel=_public(store.retry_reel(reel_id)))

    @app.post('/api/reels/<int:reel_id>/done')
    def mark_done(reel_id):
        return jsonify(reel=_public(store.mark_done(reel_id)))

    @app.post('/api/settings')
    def settings():
        body = request.get_json() or {}
        if 'interval_minutes' in body:
            try:
                minutes = int(body['interval_minutes'])
            except (TypeError, ValueError):
                raise ValueError("Interval must be a whole number of minutes")
            worker_mod.set_interval(minutes)
            the_worker.wake()
        return jsonify(schedule=worker_mod.status())

    return app


def lan_ip():
    """This PC's LAN address (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    p = argparse.ArgumentParser(description="Reel queue dashboard")
    p.add_argument('--host', default='0.0.0.0',
                   help='Address to listen on (default: all, so phones on the LAN can connect)')
    p.add_argument('--port', type=int, default=DEFAULT_PORT)
    p.add_argument('--set-password', action='store_true', help='Set the dashboard password and exit')
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('werkzeug').setLevel(logging.WARNING)   # hide per-request poll logs

    if args.set_password:
        first = getpass.getpass("New dashboard password: ")
        if first != getpass.getpass("Repeat password: "):
            sys.exit("Passwords didn't match.")
        try:
            set_password(first)
        except ValueError as e:
            sys.exit(str(e))
        print("Password saved. Existing logins were signed out.")
        return

    store.init_db()
    if not store.get_setting('password_hash'):
        sys.exit("Set a dashboard password first:  .venv/bin/python server.py --set-password")

    w = worker_mod.Worker()
    try:
        w.start()
    except RuntimeError as e:
        sys.exit(str(e))
    app = create_app(w)

    ip = lan_ip()
    print(f"\nDashboard on this PC:  http://127.0.0.1:{args.port}")
    if args.host == '0.0.0.0' and ip:
        print(f"From your phone:       http://{ip}:{args.port}")
    print("Press Ctrl+C to stop.\n")
    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    finally:
        w.stop()


if __name__ == '__main__':
    main()
