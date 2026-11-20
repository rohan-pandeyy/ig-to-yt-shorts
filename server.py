"""
server.py
---------
The dashboard: a Flask app and the background upload worker in one process.

    ./start.sh      (Linux)      start.bat      (Windows)

The start scripts set up the virtual environment and packages, then run this file, which
checks FFmpeg and credentials, asks for a dashboard password and YouTube sign-in on first
run, upgrades the database if needed, starts the worker and the web server, and prints the
local/network URLs with a QR code for your phone.

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
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import (Flask, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

import ig_to_yt_shorts as yt
import startup
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


def prompt_new_password():
    """Ask for a new dashboard password twice. Returns it, or None after 3 failed tries."""
    for _ in range(3):
        first = getpass.getpass(f"New dashboard password ({MIN_PASSWORD_LEN}+ characters): ")
        if len(first) < MIN_PASSWORD_LEN:
            print(f"Too short: use at least {MIN_PASSWORD_LEN} characters.")
            continue
        if first == getpass.getpass("Repeat password: "):
            return first
        print("Passwords didn't match.")
    return None


def _ask_yes(question):
    try:
        return input(f"{question} [Y/n] ").strip().lower() in ('', 'y', 'yes')
    except EOFError:
        return False


def first_run_setup(interactive, host, port):
    """Startup checks and first-time configuration. Returns False if the app can't start."""
    problems = startup.check_versions()
    if not startup.port_available(host, port):
        problems.append(f"Port {port} is already in use by another program. Start on another port:\n"
                        f"    start.sh --port {port + 1}      (Windows: start.bat --port {port + 1})")
    if not startup.find_ffmpeg(yt.FFMPEG_EXE):
        problems.append(startup.ffmpeg_instructions())
    secrets_problem = startup.check_client_secrets(yt.CLIENT_SECRETS_FILE)
    if secrets_problem:
        problems.append(secrets_problem)
    if problems:
        print("\nCan't start yet:\n")
        for p in problems:
            print(f"- {p}\n")
        return False

    store.init_db()   # creates queue.db or adds new columns to an existing one; keeps all data

    if not store.get_setting('password_hash'):
        if not interactive:
            print("No dashboard password set. Run the start script in a terminal to set one.")
            return False
        print("\nFirst run: choose a password for the dashboard (you'll type it on your phone).")
        password = prompt_new_password()
        if not password:
            return False
        set_password(password)
        print("Password saved.\n")

    try:
        yt.get_authenticated_service(interactive=False)
    except yt.AuthRequired:
        print("\nYouTube isn't signed in yet (or the sign-in expired).")
        if interactive and _ask_yes("Sign in now? A browser window will open."):
            yt.get_authenticated_service(interactive=True)
            print("YouTube sign-in saved.\n")
        else:
            print("Starting anyway. Uploads stay paused until you sign in "
                  "(run the start script again, or: python ig_to_yt_shorts.py --auth).\n")
    except Exception as e:   # e.g. no internet right now; the worker retries later
        print(f"\nWarning: couldn't check the YouTube sign-in ({e}). Starting anyway.\n")
    return True


def main():
    p = argparse.ArgumentParser(description="Reel queue dashboard (web page + upload worker)")
    p.add_argument('--host', default='0.0.0.0',
                   help='Address to listen on (default: all, so phones on the LAN can connect)')
    p.add_argument('--port', type=int, default=DEFAULT_PORT)
    p.add_argument('--set-password', action='store_true', help='Set the dashboard password and exit')
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)   # keep messages in order when piped/logged
    except (AttributeError, ValueError):
        pass

    if args.set_password:
        store.init_db()
        password = prompt_new_password()
        if not password:
            sys.exit(1)
        set_password(password)
        print("Password saved. Existing logins were signed out.")
        return

    # 1. Only one dashboard per PC: check before asking any setup questions.
    w = worker_mod.Worker()
    try:
        w.acquire_lock()
    except RuntimeError as e:
        sys.exit(str(e))

    try:
        # 2. Checks and first-time setup (password, YouTube sign-in, database migration).
        if not first_run_setup(sys.stdin.isatty(), args.host, args.port):
            sys.exit(1)

        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        logging.getLogger('werkzeug').setLevel(logging.WARNING)   # hide the page's 5-second polls

        # 3. Claim the port before starting uploads, so a busy port fails cleanly.
        app = create_app(w)
        try:
            httpd = make_server(args.host, args.port, app, threaded=True)
        except (OSError, SystemExit):   # werkzeug exits by itself on "address in use"
            sys.exit(f"Can't listen on port {args.port}. Start on another port: --port {args.port + 1}")

        # 4. Start the worker, then print the banner exactly once.
        w.start()
        local, network, others, warnings = startup.dashboard_urls(args.host, args.port)
        print(startup.banner(local, network, others, warnings, startup.qr_mode()), flush=True)
        try:
            httpd.serve_forever()      # returns on Ctrl+C (werkzeug handles the interrupt itself)
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        print("\nStopping...")
        if w.current_reel_id is not None:
            print("A reel was being processed. If its upload had started it will show as "
                  "'Check channel' next time; otherwise it simply goes back to the queue.")
    finally:
        w.stop()
    print("Stopped.")


if __name__ == '__main__':
    main()
