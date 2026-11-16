"""Host-aware OIDC redirect URI: allowlisted hosts only, state-bound redirect for the token exchange."""
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import auth

CFG = {
    "oidc_issuer": "https://idp.example.test/application/o/app",
    "oidc_client_id": "client-id",
    "oidc_client_secret": "synthetic-secret",
    "oidc_redirect_uri": "https://old.example.test/auth/callback",
    "oidc_extra_hosts": ["new.example.test"],
}


def req(host=None, fwd_host=None, proto="https"):
    headers = {}
    if host:
        headers["host"] = host
    if fwd_host:
        headers["x-forwarded-host"] = fwd_host
    if proto:
        headers["x-forwarded-proto"] = proto
    return SimpleNamespace(headers=headers)


def test_configured_host_keeps_configured_uri():
    assert auth.oidc_redirect_uri_for(req("old.example.test"), CFG) == CFG["oidc_redirect_uri"]


def test_allowed_extra_host_gets_its_own_callback():
    assert auth.oidc_redirect_uri_for(req("new.example.test"), CFG) == "https://new.example.test/auth/callback"


def test_forwarded_host_wins_over_host():
    r = req("127.0.0.1:8094", fwd_host="new.example.test")
    assert auth.oidc_redirect_uri_for(r, CFG) == "https://new.example.test/auth/callback"


def test_unknown_host_falls_back_to_configured():
    assert auth.oidc_redirect_uri_for(req("evil.example.test"), CFG) == CFG["oidc_redirect_uri"]
    assert auth.oidc_redirect_uri_for(req("x", fwd_host="evil.example.test"), CFG) == CFG["oidc_redirect_uri"]


def test_no_extra_hosts_means_unchanged_behaviour():
    cfg = {k: v for k, v in CFG.items() if k != "oidc_extra_hosts"}
    assert auth.oidc_redirect_uri_for(req("new.example.test"), cfg) == cfg["oidc_redirect_uri"]


def test_missing_host_falls_back():
    assert auth.oidc_redirect_uri_for(req(), CFG) == CFG["oidc_redirect_uri"]


def test_http_scheme_when_not_forwarded_as_https():
    r = req("new.example.test", proto=None)
    assert auth.oidc_redirect_uri_for(r, CFG) == "http://new.example.test/auth/callback"


def test_login_stores_redirect_and_sends_it(monkeypatch):
    monkeypatch.setattr(auth, "load_system_config", lambda: CFG)

    async def fake_discovery():
        return {"authorization_endpoint": "https://idp.example.test/authorize"}

    monkeypatch.setattr(auth, "get_discovery", fake_discovery)
    resp = asyncio.run(auth.sso_login_handler(req("new.example.test")))
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    assert q["redirect_uri"] == ["https://new.example.test/auth/callback"]
    state = q["state"][0]
    assert auth._state_redirects[state] == "https://new.example.test/auth/callback"
    assert auth._consume_state(state) is True


def test_expired_state_drops_stored_redirect(monkeypatch):
    auth._store_state("s1", "https://new.example.test/auth/callback")
    auth._pending_states["s1"] = 0
    assert auth._consume_state("s1") is False
    assert "s1" not in auth._state_redirects
