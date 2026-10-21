"""
Local (username/password) accounts for JobSearchHQ.

This is the default sign-in method: the first-run /setup page creates the admin
here, and OIDC is an optional add-on configured later from Settings.

Accounts live in /app/data/users.json (mode 0600). Passwords are hashed with
scrypt from the standard library — no extra dependencies. Local user IDs look
like "u_<hex>" so they can never collide with an OIDC `sub`, and so the logout
handler can tell the two kinds of session apart.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("JSHQ_DATA_DIR", "/app/data"))
USERS_PATH = DATA_DIR / "users.json"

ID_PREFIX = "u_"
MIN_PASSWORD_LEN = 10

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

_lock = threading.RLock()

# Failed-login throttle, in memory: key -> list of failure timestamps.
_failures: dict = {}
_MAX_FAILURES = 5
_FAILURE_WINDOW = 15 * 60


class UserError(ValueError):
    """Raised for validation problems that are safe to show to the user."""


def is_local_id(user_id: str) -> bool:
    return user_id.startswith(ID_PREFIX)


# ── Password hashing ───────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2,
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


# Verified against when the email is unknown, so a miss costs the same as a hit.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LEN} characters")


# ── Storage ────────────────────────────────────────────────────────────────────

def _load() -> dict:
    if USERS_PATH.exists():
        try:
            return json.loads(USERS_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save(users: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, USERS_PATH)


def _public(u: dict) -> dict:
    return {k: u[k] for k in ("id", "email", "name", "created_at")}


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


# ── Operations ─────────────────────────────────────────────────────────────────

def create_user(name: str, email: str, password: str) -> dict:
    name = (name or "").strip()
    email = _norm_email(email)
    if not name:
        raise UserError("Name is required")
    if "@" not in email or "|" in email:
        raise UserError("Enter a valid email address")
    validate_password(password)
    with _lock:
        users = _load()
        if any(u["email"] == email for u in users.values()):
            raise UserError("An account with that email already exists")
        user = {
            "id": ID_PREFIX + secrets.token_hex(8),
            "email": email,
            "name": name,
            "pw_hash": hash_password(password),
            "created_at": int(time.time()),
        }
        users[user["id"]] = user
        _save(users)
    return _public(user)


def exists(user_id: str) -> bool:
    return user_id in _load()


def list_users() -> list:
    return sorted((_public(u) for u in _load().values()), key=lambda u: u["created_at"])


def delete_user(user_id: str) -> bool:
    with _lock:
        users = _load()
        if user_id not in users:
            return False
        del users[user_id]
        _save(users)
    return True


def set_password(user_id: str, password: str) -> None:
    validate_password(password)
    with _lock:
        users = _load()
        if user_id not in users:
            raise UserError("User not found")
        users[user_id]["pw_hash"] = hash_password(password)
        _save(users)


def check_password(user_id: str, password: str) -> bool:
    u = _load().get(user_id)
    return bool(u and verify_password(password, u["pw_hash"]))


def throttled(key: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < _FAILURE_WINDOW]
    _failures[key] = recent
    return len(recent) >= _MAX_FAILURES


def authenticate(email: str, password: str, client: str = "") -> Optional[dict]:
    """Return the public user dict on success, None on bad credentials.

    Raises UserError when the caller has failed too many times recently.
    """
    email = _norm_email(email)
    key = f"{client}|{email}"
    if throttled(key):
        raise UserError("Too many failed attempts — wait a few minutes and try again")
    user = next((u for u in _load().values() if u["email"] == email), None)
    ok = verify_password(password, user["pw_hash"] if user else _DUMMY_HASH)
    if user and ok:
        _failures.pop(key, None)
        return _public(user)
    _failures.setdefault(key, []).append(time.time())
    return None
