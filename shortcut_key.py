"""
shortcut_key.py
---------------
The API key used by the iPhone "Add to Reel Queue" Shortcut.

- 160 random bits from the `secrets` module, written as rq-XXXX-XXXX-... (8 groups of 4)
  using letters and digits that are hard to confuse, so it can be typed if needed.
- Only a SHA-256 hash is stored (settings table in queue.db). A slow password hash isn't
  needed: the key is fully random, so it can't be guessed from its hash.
- Comparison is constant-time. Case, spaces and dashes don't matter when checking.
- The key only authorises POST /api/shortcut/add (enforced in server.py).
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

import store

PREFIX = 'rq'
ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'   # 32 symbols (no 0/O, 1/I) -> 5 bits each
GROUPS, GROUP_LEN = 8, 4                        # 32 symbols = 160 bits
HASH_SETTING = 'shortcut_key_hash'
CREATED_SETTING = 'shortcut_key_created_at'


def _normalize(key):
    return ''.join(c for c in str(key).upper() if c.isalnum())


def _hash(key):
    return hashlib.sha256(_normalize(key).encode('ascii', 'ignore')).hexdigest()


def generate():
    body = ''.join(secrets.choice(ALPHABET) for _ in range(GROUPS * GROUP_LEN))
    groups = [body[i:i + GROUP_LEN] for i in range(0, len(body), GROUP_LEN)]
    return '-'.join([PREFIX, *groups])


def create():
    """Make a new key, replacing any existing one (which stops working at once). Returns it."""
    key = generate()
    store.init_db()
    store.set_setting(HASH_SETTING, _hash(key))
    store.set_setting(CREATED_SETTING, datetime.now(timezone.utc).isoformat(timespec='seconds'))
    return key


def revoke():
    """Remove the key. Returns True if there was one."""
    store.init_db()
    had = is_configured()
    store.clear_setting(HASH_SETTING)
    store.clear_setting(CREATED_SETTING)
    return had


def is_configured():
    return bool(store.get_setting(HASH_SETTING))


def verify(presented):
    """True if `presented` is the current key. Constant-time; False when none is set."""
    stored = store.get_setting(HASH_SETTING)
    if not stored or not presented:
        return False
    return hmac.compare_digest(_hash(presented), stored)
