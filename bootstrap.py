"""
bootstrap.py
------------
Run by start.sh / start.bat with the virtual environment's Python, before server.py.
Installs requirements.txt only when it changed since the last successful install (or a
package is missing), so repeated starts are fast. Standard library only.
"""

import hashlib
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS = os.path.join(HERE, 'requirements.txt')
STAMP = os.path.join(sys.prefix, '.requirements.sha256')     # lives inside .venv
REQUIRED_MODULES = ('yt_dlp', 'flask', 'googleapiclient', 'google_auth_oauthlib', 'qrcode')


def requirements_hash(path=REQUIREMENTS):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def needs_install(path=REQUIREMENTS, stamp=STAMP, find_spec=importlib.util.find_spec):
    try:
        with open(stamp, encoding='utf-8') as f:
            if f.read().strip() != requirements_hash(path):
                return True
    except OSError:
        return True
    return any(find_spec(m) is None for m in REQUIRED_MODULES)


def main():
    if sys.prefix == sys.base_prefix:
        sys.exit("bootstrap.py must be run with the project's .venv Python (use start.sh / start.bat).")
    if not needs_install():
        return
    print("Installing Python packages (first run or requirements changed)...")
    result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                             '-q', '-r', REQUIREMENTS])
    if result.returncode != 0:
        sys.exit("\nInstalling packages failed (see the error above). Check your internet "
                 "connection and run the start script again.")
    with open(STAMP, 'w', encoding='utf-8') as f:
        f.write(requirements_hash())
    print("Packages installed.")


if __name__ == '__main__':
    main()
