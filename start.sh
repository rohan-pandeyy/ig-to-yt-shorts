#!/usr/bin/env bash
# One-command start for Linux: sets up .venv, installs packages only when needed, then runs
# the dashboard (web page + upload worker) via server.py. Safe to run repeatedly.
# Extra arguments go to server.py, e.g.:  ./start.sh --port 8081
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

MIN_CHECK='import sys; sys.exit(sys.version_info < (3, 11))'
VENV_PY=".venv/bin/python"

find_python() {
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$MIN_CHECK" 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

if [ -e "$VENV_PY" ] && ! "$VENV_PY" -c "$MIN_CHECK" >/dev/null 2>&1; then
    echo "The existing .venv is broken or uses an old Python; recreating it (your data is not touched)."
    RECREATE=1
fi

if [ ! -e "$VENV_PY" ] || [ "${RECREATE:-0}" = 1 ]; then
    if ! PY=$(find_python); then
        echo "Python 3.11 or newer was not found. Install it with your package manager, e.g.:"
        echo "    sudo dnf install python3      (Fedora)"
        echo "    sudo apt install python3 python3-venv      (Debian/Ubuntu)"
        exit 1
    fi
    echo "Creating virtual environment in .venv using $($PY --version) ..."
    if ! "$PY" -m venv --clear .venv; then
        echo "Couldn't create the virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
        exit 1
    fi
fi

"$VENV_PY" bootstrap.py || exit 1
exec "$VENV_PY" server.py "$@"
