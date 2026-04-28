"""
OIDC authentication for JobSearchHQ.

Provider-agnostic — works with Authentik, Auth0, Keycloak, or any OIDC provider.
Config lives in /app/data/system_config.json, managed via the /setup UI.
Session: HTTP-only HMAC-signed cookie (Python stdlib only, no extra deps).

Admin: the first OIDC user to log in is automatically designated admin.
Admin user ID is stored in system_config.json under 'admin_user_id'.
"""

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from models import User

DATA_DIR = Path("/app/data")
SYSTEM_CONFIG_PATH = DATA_DIR / "system_config.json"

COOKIE_NAME = "jshq_session"
COOKIE_MAX_AGE = 86400 * 30  # 30 days

_discovery_cache: dict = {"data": None, "at": 0.0}
CACHE_TTL = 3600  # seconds


# ── System config ──────────────────────────────────────────────────────────────

def load_system_config() -> dict:
    if SYSTEM_CONFIG_PATH.exists():
        try:
            return json.loads(SYSTEM_CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_system_config(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not config.get("session_secret"):
        config["session_secret"] = secrets.token_hex(32)
    SYSTEM_CONFIG_PATH.write_text(json.dumps(config, indent=2))


def is_setup_complete() -> bool:
    cfg = load_system_config()
    return bool(
        cfg.get("oidc_issuer")
        and cfg.get("oidc_client_id")
        and cfg.get("oidc_client_secret")
        and cfg.get("oidc_redirect_uri")
    )


def get_session_secret() -> str:
    cfg = load_system_config()
    secret = cfg.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        cfg["session_secret"] = secret
        save_system_config(cfg)
    return secret


# ── Admin user helpers ─────────────────────────────────────────────────────────

def get_admin_user_id() -> Optional[str]:
    """Return the designated admin OIDC sub, or None if not yet set."""
    return load_system_config().get("admin_user_id")


def set_admin_user_id(user_id: str) -> None:
    """Persist the admin OIDC sub to system_config.json."""
    cfg = load_system_config()
    cfg["admin_user_id"] = user_id
    save_system_config(cfg)


def is_admin(user_id: str) -> bool:
    """Return True if this user is the designated admin."""
    admin_id = get_admin_user_id()
    return bool(admin_id and admin_id == user_id)


# ── Cookie helpers ─────────────────────────────────────────────────────────────

def _sign(value: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def _unsign(signed: str, secret: str) -> Optional[str]:
    if "." not in signed:
        return None
    value, _, sig = signed.rpartition(".")
    expected = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return value


def set_session_cookie(response, user_id: str, email: str, name: str) -> None:
    secret = get_session_secret()
    safe_name = name.replace("|", " ").replace("\n", " ").strip()
    safe_email = email.replace("|", "").replace("\n", "").strip()
    payload = f"{user_id}|{safe_email}|{safe_name}"
    signed = _sign(payload, secret)
    response.set_cookie(
        COOKIE_NAME, signed,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set True when running behind HTTPS
    )


def get_session_from_cookie(request: Request) -> Optional[dict]:
    signed = request.cookies.get(COOKIE_NAME)
    if not signed:
        return None
    try:
        secret = get_session_secret()
        payload = _unsign(signed, secret)
        if not payload:
            return None
        user_id, email, name = payload.split("|", 2)
        return {"user_id": user_id, "email": email, "name": name}
    except Exception:
        return None


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax")


# ── OIDC discovery (cached) ────────────────────────────────────────────────────

async def get_discovery() -> dict:
    now = time.monotonic()
    if _discovery_cache["data"] and now - _discovery_cache["at"] < CACHE_TTL:
        return _discovery_cache["data"]
    cfg = load_system_config()
    issuer = cfg.get("oidc_issuer", "").rstrip("/")
    if not issuer:
        raise HTTPException(503, "OIDC not configured")
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{issuer}/.well-known/openid-configuration", timeout=10
        )
        r.raise_for_status()
        _discovery_cache["data"] = r.json()
        _discovery_cache["at"] = now
    return _discovery_cache["data"]


def invalidate_discovery_cache() -> None:
    """Call after saving new OIDC config so next request re-fetches."""
    _discovery_cache["data"] = None
    _discovery_cache["at"] = 0.0


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_current_user(request: Request) -> User:
    if not is_setup_complete():
        raise HTTPException(503, detail="setup_required")
    session = get_session_from_cookie(request)
    if not session:
        raise HTTPException(401, detail="not_authenticated")
    return User(
        id=session["user_id"],
        email=session["email"],
        name=session["name"],
    )


# ── Route handlers (mounted in main.py) ───────────────────────────────────────

async def login_handler(request: Request):
    if not is_setup_complete():
        return RedirectResponse("/setup")
    cfg = load_system_config()
    discovery = await get_discovery()
    state = secrets.token_urlsafe(32)
    params = (
        f"?client_id={cfg['oidc_client_id']}"
        f"&redirect_uri={cfg['oidc_redirect_uri']}"
        f"&response_type=code"
        f"&scope=openid+profile+email"
        f"&state={state}"
    )
    response = RedirectResponse(discovery["authorization_endpoint"] + params)
    response.set_cookie("oidc_state", state, httponly=True, max_age=600, samesite="lax")
    return response


async def callback_handler(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    stored_state = request.cookies.get("oidc_state")

    if not code:
        raise HTTPException(400, "Missing authorization code")
    if not state or state != stored_state:
        raise HTTPException(400, "Invalid state — possible CSRF")

    cfg = load_system_config()
    discovery = await get_discovery()

    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_resp = await client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": cfg["oidc_redirect_uri"],
                "client_id": cfg["oidc_client_id"],
                "client_secret": cfg["oidc_client_secret"],
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()

        # Fetch canonical user info from userinfo endpoint
        userinfo_resp = await client.get(
            discovery["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            timeout=10,
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()

    user_id = userinfo.get("sub", "")
    email = userinfo.get("email", "")
    name = (
        userinfo.get("name")
        or userinfo.get("preferred_username")
        or email
    )

    if not user_id:
        raise HTTPException(400, "Could not extract user identity from OIDC provider")

    # ── Auto-designate admin: first user to log in becomes the admin ──────────
    if not get_admin_user_id():
        set_admin_user_id(user_id)

    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("oidc_state")
    set_session_cookie(response, user_id, email, name)
    return response


async def logout_handler(request: Request):
    response = RedirectResponse("/", status_code=302)
    clear_session_cookie(response)
    return response
