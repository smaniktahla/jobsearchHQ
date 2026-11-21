"""Gemini model discovery, resilient fallback, and the model-list endpoint used by the wizard/Settings pickers."""
import io
import json
import types
import urllib.error

import pytest
from fastapi.testclient import TestClient

import ai_router
import auth
import main
import storage


def fake_models_response(names):
    body = {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"], "displayName": n}
                       for n in names]}
    return io.BytesIO(json.dumps(body).encode())


@pytest.fixture(autouse=True)
def clean_caches():
    ai_router._gemini_model_cache.clear()
    ai_router._gemini_bad.clear()
    yield
    ai_router._gemini_model_cache.clear()
    ai_router._gemini_bad.clear()


NAMES = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite-preview",
         "gemini-3.8-flash-tts", "gemini-2.5-flash-image", "gemini-flash-latest", "gemini-2.5-pro"]


def test_discovery_picks_newest_stable_flash_and_flash_lite(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_router.urllib.request, "urlopen", lambda req, timeout=0: calls.append(1) or fake_models_response(NAMES))
    found = ai_router._discover_gemini_models("k1")
    assert found["strong"] == ["gemini-3.8-flash", "gemini-3.7-flash"]      # newest first; preview/tts/image excluded
    assert found["fast"] == ["gemini-3.5-flash-lite"]
    ai_router._discover_gemini_models("k1")
    assert len(calls) == 1                                                    # cached


def test_discovery_failure_falls_back_to_latest_aliases(monkeypatch):
    def boom(req, timeout=0):
        raise OSError("offline")
    monkeypatch.setattr(ai_router.urllib.request, "urlopen", boom)
    assert ai_router._discover_gemini_models("k2") == {}
    cfg = types.SimpleNamespace(gemini_api_key="k2", fast_model="", strong_model="")
    assert ai_router._resolve_model("gemini", "strong", cfg) == "gemini-flash-latest"
    assert ai_router._resolve_model("gemini", "fast", cfg) == "gemini-flash-lite-latest"


def test_pinned_model_wins_and_is_never_swapped(monkeypatch):
    cfg = types.SimpleNamespace(gemini_api_key="k", fast_model="", strong_model="gemini-3.5-flash")
    assert ai_router._gemini_candidates("strong", cfg) == ["gemini-3.5-flash"]


def test_fallback_skips_failed_models_and_remembers_them(monkeypatch):
    monkeypatch.setattr(ai_router, "_discover_gemini_models", lambda k: {"strong": ["m3", "m2", "m1"], "fast": []})
    cfg = types.SimpleNamespace(gemini_api_key="k", fast_model="", strong_model="")
    tried = []

    def fake_chat(system, user, model, key, max_tokens, timeout=120):
        tried.append(model)
        if model == "m3":
            raise TimeoutError("504 deadline")
        return "OK"

    monkeypatch.setattr(ai_router, "_gemini_chat", fake_chat)
    out = ai_router._gemini_chat_with_fallback("s", "u", "strong", cfg, "k", 1000)
    assert out == "OK" and tried == ["m3", "m2"] and ai_router.get_last_model("strong") == "gemini/m2"
    tried.clear()
    ai_router._gemini_chat_with_fallback("s", "u", "strong", cfg, "k", 1000)
    assert tried == ["m2"]                           # m3 is cooling down, not retried


def test_pinned_model_failure_is_reported_not_hidden(monkeypatch):
    cfg = types.SimpleNamespace(gemini_api_key="k", fast_model="", strong_model="gemini-x")
    monkeypatch.setattr(ai_router, "_gemini_chat", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("slow")))
    with pytest.raises(TimeoutError):
        ai_router._gemini_chat_with_fallback("s", "u", "strong", cfg, "k", 1000)


def test_list_models_rejected_key_is_a_clear_error(monkeypatch):
    def deny(req, timeout=0):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    monkeypatch.setattr(ai_router.urllib.request, "urlopen", deny)
    with pytest.raises(ai_router.ProviderConfigError, match="rejected"):
        ai_router.list_models("gemini", "bad")


def test_list_models_gemini_shape(monkeypatch):
    monkeypatch.setattr(ai_router.urllib.request, "urlopen", lambda req, timeout=0: fake_models_response(NAMES))
    out = ai_router.list_models("gemini", "k3")
    ids = [m["id"] for m in out["models"]]
    assert "gemini-3.8-flash" in ids and "gemini-3.8-flash-tts" not in ids and "gemini-2.5-flash-image" not in ids
    assert out["recommended"] == {"fast": "gemini-3.5-flash-lite", "strong": "gemini-3.8-flash"}


# ── endpoints ────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "is_setup_complete", lambda: True)
    monkeypatch.setattr(auth, "get_session_secret", lambda: "synthetic-session-secret")
    monkeypatch.setattr(main.scheduler, "update_schedule", lambda *a: None)
    return TestClient(main.app)


def login(client, uid="amit"):
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    r = Response()
    auth.set_session_cookie(r, uid, f"{uid}@example.test", uid)
    client.cookies.set(auth.COOKIE_NAME, SimpleCookie(r.headers["set-cookie"])[auth.COOKIE_NAME].value)


def test_models_endpoint_requires_login_and_returns_models(client, monkeypatch):
    assert client.post("/api/ai/models", json={"provider": "gemini", "api_key": "k"}).status_code == 401
    login(client)
    monkeypatch.setattr(ai_router, "list_models", lambda p, k, b="": {"models": [{"id": "m", "label": "m"}], "recommended": {}})
    r = client.post("/api/ai/models", json={"provider": "gemini", "api_key": "k"})
    assert r.status_code == 200 and r.json()["models"][0]["id"] == "m"


def test_models_endpoint_maps_provider_errors_to_400(client, monkeypatch):
    login(client)
    def reject(*a, **k):
        raise ai_router.ProviderConfigError("The provider rejected this API key.")
    monkeypatch.setattr(ai_router, "list_models", reject)
    r = client.post("/api/ai/models", json={"provider": "gemini", "api_key": "bad"})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]


def test_onboarding_saves_the_chosen_model_for_both_tiers(client):
    login(client)
    assert client.put("/api/onboarding/ai", json={"provider": "gemini", "api_key": "k", "model": "gemini-3.5-flash"}).status_code == 200
    c = storage.load_config("amit")
    assert c.fast_provider == c.strong_provider == "gemini" and c.fast_model == c.strong_model == "gemini-3.5-flash"
    assert client.put("/api/onboarding/ai", json={"provider": "gemini", "api_key": "k"}).status_code == 200
    assert storage.load_config("amit").strong_model == ""            # blank = automatic


def test_provider_test_uses_a_token_budget_thinking_models_can_finish_in():
    import inspect
    src = inspect.getsource(main.test_ai_provider)
    assert "max_tokens=1000" in src
