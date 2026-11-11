"""Languages on the profile: round trip through /api/profile, agent profile, and survive a Settings save."""
import importlib, sys
from models import AppConfig, Language, ProfileUpdate, LANGUAGE_LEVELS

def test_defaults_and_levels():
    assert AppConfig().languages == []
    assert LANGUAGE_LEVELS == ["Beginner", "Intermediate", "Proficient", "Fluent", "Native"]
    assert Language(language="French").speaking == "Beginner"

def test_profile_update_accepts_languages():
    u = ProfileUpdate(languages=[{"language": "Hindi", "speaking": "Proficient", "reading": "Beginner", "writing": "Beginner"}])
    assert u.languages[0].language == "Hindi" and u.languages[0].speaking == "Proficient"
    assert ProfileUpdate().languages is None            # omitted = leave stored value alone

def test_config_roundtrip():
    c = AppConfig(languages=[Language(language="Japanese")])
    assert AppConfig(**c.model_dump()).languages[0].language == "Japanese"


# --- API behaviour (needs the app's full dependencies; runs in the container image) ---
def _client(monkeypatch):
    import auth, main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, 'is_setup_complete', lambda: True)
    monkeypatch.setattr(auth, 'get_session_secret', lambda: 'synthetic-session-secret')
    monkeypatch.setattr(main.scheduler, 'update_schedule', lambda *a: None)
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    c = TestClient(main.app); r = Response()
    auth.set_session_cookie(r, 'lang-user', 'l@example.test', 'L')
    c.cookies.set(auth.COOKIE_NAME, SimpleCookie(r.headers['set-cookie'])[auth.COOKIE_NAME].value)
    return c

def test_api_roundtrip_and_settings_save_keeps_languages(monkeypatch):
    import pytest; pytest.importorskip("jobspy")
    c = _client(monkeypatch)
    langs = [{"language": "Hindi", "speaking": "Proficient", "reading": "Beginner", "writing": "Beginner"}]
    r = c.put("/api/profile", json={"languages": langs}); assert r.status_code == 200
    assert r.json()["languages"] == langs
    assert c.get("/api/profile").json()["languages"] == langs
    c.put("/api/profile", json={"phone": "555"})                       # omitted languages: untouched
    assert c.get("/api/profile").json()["languages"] == langs
    cfg = c.get("/api/config").json()
    assert c.put("/api/config", json=cfg).status_code == 200             # Settings save must not wipe them
    assert c.get("/api/profile").json()["languages"] == langs
    c.put("/api/profile", json={"languages": []})                        # explicit empty clears
    assert c.get("/api/profile").json()["languages"] == []
