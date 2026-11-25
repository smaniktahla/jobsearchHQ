"""
Authentication for JobSearchHQ.

Default: local email/password accounts (see local_users.py). The first-run
/setup page creates the admin account — no identity provider needed.

Optional: OIDC single sign-on, provider-agnostic (Authentik, Auth0, Keycloak...).
Configured later by the admin from Settings; config lives in
/app/data/system_config.json. When enabled, the login page also offers SSO.

Session: HTTP-only HMAC-signed cookie (Python stdlib only, no extra deps).

Admin user ID is stored in system_config.json under 'admin_user_id'. Legacy
installs that configured OIDC before any admin existed still promote the first
OIDC login to admin.
"""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit
from typing import Literal, Optional

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
import local_users
from models import AgentEvent, AgentEventType, User

DATA_DIR = Path(os.environ.get("JSHQ_DATA_DIR", "/app/data"))
SYSTEM_CONFIG_PATH = DATA_DIR / "system_config.json"

COOKIE_NAME = "jshq_session"
COOKIE_MAX_AGE = 86400 * 30  # 30 days

_discovery_cache: dict = {"data": None, "at": 0.0}
CACHE_TTL = 3600  # seconds

# Server-side OAuth state store — avoids relying on oidc_state cookie surviving
# reverse-proxy hops. Keyed by state value, value is expiry timestamp.
_pending_states: dict = {}
_STATE_TTL = 600  # 10 minutes

# In-memory ID token store for RP-initiated logout (id_token_hint).
# Keyed by user_id (OIDC sub). Lost on restart, which is fine.
_id_tokens: dict = {}


def store_id_token(user_id: str, id_token: str) -> None:
    _id_tokens[user_id] = id_token


def get_id_token(user_id: str) -> Optional[str]:
    return _id_tokens.get(user_id)


# redirect_uri used when a login started, keyed by state; the token exchange must
# present exactly the same value (RFC 6749 4.1.3).
_state_redirects: dict = {}


def _store_state(state: str, redirect_uri: Optional[str] = None) -> None:
    _pending_states[state] = time.time() + _STATE_TTL
    if redirect_uri:
        _state_redirects[state] = redirect_uri
    expired = [k for k, v in list(_pending_states.items()) if v < time.time()]
    for k in expired:
        del _pending_states[k]
        _state_redirects.pop(k, None)


def _consume_state(state: str) -> bool:
    """Return True and remove state if valid and unexpired."""
    expires = _pending_states.get(state)
    if not expires or time.time() > expires:
        _state_redirects.pop(state, None)
        return False
    del _pending_states[state]
    return True


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def _request_host(request: Request) -> str:
    """Host the browser used (reverse proxies pass it as X-Forwarded-Host)."""
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return raw.split(",")[0].strip().lower()


def oidc_redirect_uri_for(request: Request, cfg: dict) -> str:
    """Redirect URI for this login: the configured one, unless the browser came in on
    another hostname that is explicitly allowed (cfg["oidc_extra_hosts"]). Unknown
    hosts always fall back to the configured URI, so a spoofed Host header cannot
    steer the callback anywhere unregistered. Each allowed host must also be
    registered as a redirect URI at the identity provider."""
    configured = cfg.get("oidc_redirect_uri", "")
    host = _request_host(request)
    if not host:
        return configured
    configured_host = urlsplit(configured).netloc.lower()
    extras = {str(h).strip().lower() for h in (cfg.get("oidc_extra_hosts") or []) if str(h).strip()}
    if host == configured_host or host not in extras:
        return configured
    scheme = "https" if _is_https(request) else "http"
    return f"{scheme}://{host}/auth/callback"


# ── System config ──────────────────────────────────────────────────────────────

_system_config_lock = threading.Lock()


def load_system_config() -> dict:
    if SYSTEM_CONFIG_PATH.exists():
        try:
            return json.loads(SYSTEM_CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_system_config(config: dict) -> None:
    SYSTEM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not config.get("session_secret"):
        config["session_secret"] = secrets.token_hex(32)
    with tempfile.NamedTemporaryFile(mode="w", dir=SYSTEM_CONFIG_PATH.parent, delete=False) as temp:
        path = Path(temp.name)
        try:
            json.dump(config, temp, indent=2)
            temp.close()
            path.replace(SYSTEM_CONFIG_PATH)
        finally:
            path.unlink(missing_ok=True)


@contextmanager
def edit_system_config():
    """Serialize read-modify-write updates in the app process."""
    with _system_config_lock:
        config = load_system_config()
        original = config.copy()
        yield config
        if config != original:
            save_system_config(config)


def is_oidc_configured() -> bool:
    cfg = load_system_config()
    return bool(
        cfg.get("oidc_issuer")
        and cfg.get("oidc_client_id")
        and cfg.get("oidc_client_secret")
        and cfg.get("oidc_redirect_uri")
    )


def is_setup_complete() -> bool:
    """Setup is done once an admin exists (or a legacy OIDC config is in place)."""
    return bool(get_admin_user_id()) or is_oidc_configured()


def setup_token_required() -> bool:
    """Operators can set JSHQ_SETUP_TOKEN to gate first-run admin creation."""
    return bool(os.environ.get("JSHQ_SETUP_TOKEN"))


_setup_lock = threading.Lock()


def bootstrap_admin(name: str, email: str, password: str, setup_token: str = "") -> dict:
    """Create the first admin account. Only works while no admin exists."""
    expected = os.environ.get("JSHQ_SETUP_TOKEN", "")
    if expected and not hmac.compare_digest(setup_token or "", expected):
        raise HTTPException(403, "Invalid setup token")
    with _setup_lock:
        if get_admin_user_id():
            raise HTTPException(403, "Setup is already complete")
        try:
            user = local_users.create_user(name, email, password)
        except local_users.UserError as exc:
            raise HTTPException(400, str(exc)) from exc
        set_admin_user_id(user["id"])
    return user


def get_session_secret() -> str:
    return _get_system_token("session_secret")


# ── Admin user helpers ─────────────────────────────────────────────────────────

def get_admin_user_id() -> Optional[str]:
    """Return the designated admin OIDC sub, or None if not yet set."""
    return load_system_config().get("admin_user_id")


def set_admin_user_id(user_id: str) -> None:
    """Persist the admin OIDC sub to system_config.json."""
    with edit_system_config() as cfg:
        cfg["admin_user_id"] = user_id


def is_admin(user_id: str) -> bool:
    """Return True if this user is the designated admin."""
    admin_id = get_admin_user_id()
    return bool(admin_id and admin_id == user_id)


# ── Agent API key ──────────────────────────────────────────────────────────────

def _get_system_token(field: str, *, rotate: bool = False) -> str:
    with edit_system_config() as cfg:
        token = cfg.get(field)
        if rotate or not token:
            token = cfg[field] = secrets.token_hex(32)
        return token


def get_agent_api_key() -> str:
    """Return the management key, creating it if absent."""
    return _get_system_token("agent_api_key")


def get_executor_api_key(*, rotate: bool = False) -> str:
    """Return the executor key, creating or replacing it on admin request."""
    return _get_system_token("executor_api_key", rotate=rotate)


def verify_agent_api_key(key: str) -> bool:
    """Verify the legacy management key."""
    return _matches_key(key.encode(), load_system_config().get("agent_api_key"))


def _matches_key(supplied: bytes, stored: str | None) -> bool:
    return bool(stored) and hmac.compare_digest(supplied, stored.encode())


AgentScope = Literal["management", "executor_read", "executor_write"]
_agent_audit_lock = threading.Lock()


def require_agent_scope(request: Request, scope: AgentScope) -> str:
    """Authorize one route's API-key scope and return the admin user ID."""
    key = request.headers.get("X-API-Key", "").encode()
    config = load_system_config()
    if not key:
        raise HTTPException(401, "Invalid or missing API key")
    management = _matches_key(key, config.get("agent_api_key"))
    executor = _matches_key(key, config.get("executor_api_key"))
    if not (management or executor):
        raise HTTPException(401, "Invalid or missing API key")
    if not management and scope not in ("executor_read", "executor_write"):
        event = AgentEvent(
            event_type=AgentEventType.SCOPE_DENIED,
            success=False,
            notes=f"Executor key denied {request.method} {request.scope['route'].path} (requires {scope})",
        )
        with _agent_audit_lock:
            with (DATA_DIR / "agent_scope_audit.jsonl").open("a", encoding="utf-8") as audit:
                audit.write(event.model_dump_json() + "\n")
        raise HTTPException(403, "API key scope does not permit this operation")
    admin_id = config.get("admin_user_id")
    if not admin_id:
        raise HTTPException(503, "No admin user configured")
    return admin_id


def agent_scope(scope: AgentScope):
    """Bind a route's scope without exposing it as a request parameter."""
    def check(request: Request) -> str:
        return require_agent_scope(request, scope)

    return Depends(check)


# ── Digest one-click action tokens ─────────────────────────────────────────────
# Lets a digest email's Approve/Pass buttons set review_status without requiring
# the recipient to be logged in. Separate secret from session_secret/agent_api_key
# so a leaked digest link can't be used to forge a session or agent calls.

def get_digest_action_secret() -> str:
    return _get_system_token("digest_action_secret")


def sign_digest_action(user_id: str, job_id: str, action: str, ttl_seconds: int = 14 * 86400) -> str:
    """action must be 'approve' or 'reject'. Token expires after ttl_seconds (default 14 days)."""
    expires = int(time.time()) + ttl_seconds
    payload = f"{user_id}|{job_id}|{action}|{expires}"
    return _sign(payload, get_digest_action_secret())


def verify_digest_action(token: str) -> Optional[dict]:
    """Verify and decode a digest action token. Returns None if invalid, tampered, or expired."""
    payload = _unsign(token, get_digest_action_secret())
    if not payload:
        return None
    parts = payload.split("|", 3)
    if len(parts) != 4:
        return None
    user_id, job_id, action, expires = parts
    if action not in ("approve", "reject"):
        return None
    try:
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    return {"user_id": user_id, "job_id": job_id, "action": action}


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


def set_session_cookie(response, user_id: str, email: str, name: str, secure: bool = False) -> None:
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
        secure=secure,
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


def clear_session_cookie(response, secure: bool = False) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax", secure=secure)


# ── OIDC discovery (cached) ────────────────────────────────────────────────────

async def get_discovery() -> dict:
    now = time.monotonic()
    if _discovery_cache["data"] and now - _discovery_cache["at"] < CACHE_TTL:
        return _discovery_cache["data"]
    cfg = load_system_config()
    issuer = cfg.get("oidc_issuer", "").rstrip("/")
    if not issuer:
        raise HTTPException(503, "OIDC not configured")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{issuer}/.well-known/openid-configuration", timeout=10
            )
            r.raise_for_status()
            _discovery_cache["data"] = r.json()
            _discovery_cache["at"] = now
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"Identity provider unreachable — try again in a moment ({exc})") from exc
    return _discovery_cache["data"]


def invalidate_discovery_cache() -> None:
    """Call after saving new OIDC config so next request re-fetches."""
    _discovery_cache["data"] = None
    _discovery_cache["at"] = 0.0


def session_is_valid(session: dict) -> bool:
    """A local user's session dies with their account; OIDC sessions are provider-managed."""
    uid = session["user_id"]
    return not local_users.is_local_id(uid) or local_users.exists(uid)


def require_admin(user: User) -> User:
    if not is_admin(user.id):
        raise HTTPException(403, "Admin only")
    return user


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_current_user(request: Request) -> User:
    if not is_setup_complete():
        raise HTTPException(503, detail="setup_required")
    session = get_session_from_cookie(request)
    if not session or not session_is_valid(session):
        raise HTTPException(401, detail="not_authenticated")
    return User(
        id=session["user_id"],
        email=session["email"],
        name=session["name"],
    )


def require_admin_session(user: User = Depends(get_current_user)) -> User:
    return require_admin(user)


# ── Route handlers (mounted in main.py) ───────────────────────────────────────

async def login_handler(request: Request):
    """Local sign-in page (also lists SSO when OIDC is enabled)."""
    if not is_setup_complete():
        return RedirectResponse("/setup")
    return FileResponse("static/login.html")


async def local_login_handler(request: Request):
    body = await request.json()
    client = request.client.host if request.client else ""
    try:
        user = local_users.authenticate(body.get("email", ""), body.get("password", ""), client)
    except local_users.UserError as exc:
        raise HTTPException(429, str(exc)) from exc
    if not user:
        raise HTTPException(401, "Incorrect email or password")
    response = JSONResponse({"ok": True})
    set_session_cookie(response, user["id"], user["email"], user["name"], secure=_is_https(request))
    return response


async def sso_login_handler(request: Request):
    if not is_oidc_configured():
        raise HTTPException(404, "SSO is not configured")
    cfg = load_system_config()
    discovery = await get_discovery()
    state = secrets.token_urlsafe(32)
    redirect_uri = oidc_redirect_uri_for(request, cfg)
    params = (
        f"?client_id={cfg['oidc_client_id']}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&response_type=code"
        f"&scope=openid+profile+email"
        f"&state={state}"
    )
    _store_state(state, redirect_uri)
    response = RedirectResponse(discovery["authorization_endpoint"] + params)
    return response


async def callback_handler(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code:
        raise HTTPException(400, "Missing authorization code")
    if not state or not _consume_state(state):
        raise HTTPException(400, "Invalid state — possible CSRF")

    cfg = load_system_config()
    discovery = await get_discovery()
    redirect_uri = _state_redirects.pop(state, None) or cfg["oidc_redirect_uri"]

    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_resp = await client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": cfg["oidc_client_id"],
                "client_secret": cfg["oidc_client_secret"],
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()
        print(f"[CALLBACK] token exchange OK, token_endpoint={discovery['token_endpoint']!r}", flush=True)

        # Fetch canonical user info from userinfo endpoint
        userinfo_resp = await client.get(
            discovery["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            timeout=10,
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()

    print(f"[OIDC] claims={list(userinfo.keys())} sub={userinfo.get('sub')!r} email={userinfo.get('email')!r} name={userinfo.get('name')!r} preferred_username={userinfo.get('preferred_username')!r}", flush=True)

    user_id = userinfo.get("sub", "")
    email = userinfo.get("email", "")
    name = (
        userinfo.get("name")
        or userinfo.get("preferred_username")
        or email
    )

    if not user_id:
        raise HTTPException(400, "Could not extract user identity from OIDC provider")

    # Store ID token so logout can pass id_token_hint to Authentik's end-session endpoint
    if tokens.get("id_token"):
        store_id_token(user_id, tokens["id_token"])

    # ── Auto-designate admin: first user to log in becomes the admin ──────────
    if not get_admin_user_id():
        set_admin_user_id(user_id)

    secure = _is_https(request)
    print(f"[CALLBACK] setting cookie: user_id={user_id!r} email={email!r} name={name!r} secure={secure}", flush=True)
    response = RedirectResponse("/", status_code=302)
    set_session_cookie(response, user_id, email, name, secure=secure)
    return response


async def logout_handler(request: Request):
    import logging as _logging
    _log = _logging.getLogger(__name__)
    redirect_target = "/auth/login"
    session = get_session_from_cookie(request)
    is_oidc_session = bool(session) and not local_users.is_local_id(session["user_id"])
    try:
        if not (is_oidc_session and is_oidc_configured()):
            raise LookupError("local session — nothing to end at the provider")
        discovery = await get_discovery()
        end_session = discovery.get("end_session_endpoint")
        if end_session:
            cfg = load_system_config()
            base_url = oidc_redirect_uri_for(request, cfg).rsplit("/auth/callback", 1)[0]
            from urllib.parse import urlencode
            logout_params: dict = {"post_logout_redirect_uri": base_url + "/auth/login"}
            # Pass id_token_hint so Authentik completes RP-initiated logout and redirects back
            if session:
                id_token = get_id_token(session["user_id"])
                if id_token:
                    logout_params["id_token_hint"] = id_token
            logout_params["client_id"] = cfg["oidc_client_id"]
            redirect_target = f"{end_session}?{urlencode(logout_params)}"
            _log.info("Logout: RP-initiated to %s", end_session)
    except LookupError:
        pass
    except Exception as exc:
        _log.warning("Logout: RP-initiated logout failed (%s), falling back to /auth/login", exc)
    response = RedirectResponse(redirect_target, status_code=302)
    clear_session_cookie(response, secure=True)
    clear_session_cookie(response, secure=False)
    return response
