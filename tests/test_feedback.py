"""In-app feedback: optional feature, forwards to a ticket service, never leaks it, requester comes from the session."""
import io
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import auth
import feedback
import main

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text()
URL, TOKEN = "http://tickets.internal.example:8700", "secret-service-token-123"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeATQ:
    """Stands in for feedback._atq; records calls and returns queued responses (or raises)."""
    def __init__(self):
        self.calls, self.queue = [], []

    async def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        item = self.queue.pop(0) if self.queue else httpx.Response(201, json={"id": "FB-0007", "status": "proposed"})
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("ATQ_TICKETS_URL", URL)
    monkeypatch.setenv("ATQ_TICKETS_TOKEN", TOKEN)
    feedback._recent.clear()
    f = FakeATQ()
    monkeypatch.setattr(feedback, "_atq", f)
    return f


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "is_setup_complete", lambda: True)
    monkeypatch.setattr(auth, "get_session_secret", lambda: "synthetic-session-secret")
    monkeypatch.setattr(main.scheduler, "update_schedule", lambda *a: None)
    return TestClient(main.app)


def login(client, uid="amit", name="Amit Example", email="amit@example.test"):
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    r = Response()
    auth.set_session_cookie(r, uid, email, name)
    client.cookies.set(auth.COOKIE_NAME, SimpleCookie(r.headers["set-cookie"])[auth.COOKIE_NAME].value)


def send(client, **over):
    data = {"type": "bug", "message": "The Save button does nothing", "context": json.dumps({"page": "Tracker"})}
    data.update(over)
    return client.post("/api/feedback", data=data)


# ── off by default ───────────────────────────────────────────────────────────
def test_feature_is_off_unless_configured(client, monkeypatch):
    monkeypatch.delenv("ATQ_TICKETS_URL", raising=False)
    monkeypatch.delenv("ATQ_TICKETS_TOKEN", raising=False)
    login(client)
    assert client.get("/api/feedback/status").json() == {"enabled": False}
    assert send(client).status_code == 503
    assert client.get("/api/feedback/mine").status_code == 503
    monkeypatch.setenv("ATQ_TICKETS_URL", URL)               # one of two is still off
    assert client.get("/api/feedback/status").json() == {"enabled": False}


def test_requires_login(client, fake):
    assert client.get("/api/feedback/status").status_code == 401
    assert send(client).status_code == 401
    assert client.get("/api/feedback/mine").status_code == 401


def test_status_when_configured(client, fake):
    login(client)
    assert client.get("/api/feedback/status").json() == {"enabled": True}


# ── submitting ───────────────────────────────────────────────────────────────
def test_submit_forwards_a_ticket_with_the_requester_from_the_session(client, fake):
    login(client, uid="sso-123", name="Amit Example", email="amit@example.test")
    r = client.post("/api/feedback", data={"type": "bug", "message": "The Save button does nothing",
                                           "context": json.dumps({"page": "Tracker", "viewport": "1400x900"}),
                                           "requester_ref": "someone-else", "project": "hacked"})
    assert r.status_code == 201 and r.json() == {"id": "FB-0007", "status": "received"}
    method, path, kw = fake.calls[0]
    d = kw["data"]
    assert (method, path) == ("POST", "/tickets") and kw["files"] is None
    assert d["requester_ref"] == "jshq:sso-123" and d["project"] == "jobsearchHQ"       # client input cannot choose these
    assert d["type"] == "bug" and d["title"] == d["body"] == "The Save button does nothing"
    assert d["requester_contact"] == "Amit Example <amit@example.test>"
    ctx = json.loads(d["context"])
    assert ctx["page"] == "Tracker" and ctx["viewport"] == "1400x900" and ctx["submitted_via"] == "jshq-help-drawer"


def test_validation(client, fake):
    login(client)
    assert send(client, type="rant").status_code == 400
    assert send(client, message="   ").status_code == 400
    assert send(client, message="x" * 5001).status_code == 400
    assert fake.calls == []                                   # nothing reached the ticket service
    r = send(client, context="not json at all")
    assert r.status_code == 201 and json.loads(fake.calls[0][2]["data"]["context"]) == {"submitted_via": "jshq-help-drawer"}


def test_long_messages_get_a_short_title_and_keep_their_full_text(client, fake):
    login(client)
    msg = "Line one about the tracker " * 10 + "\nsecond line"
    send(client, message=msg)
    d = fake.calls[0][2]["data"]
    assert len(d["title"]) <= 80 and d["title"].endswith("…") and "\n" not in d["title"] and d["body"] == msg.strip()


def test_screenshots_need_consent_and_are_checked(client, fake, monkeypatch):
    login(client)
    shot = {"screenshot": ("s.png", io.BytesIO(PNG), "image/png")}
    assert client.post("/api/feedback", data={"type": "bug", "message": "m"}, files=shot).status_code == 400   # no consent
    assert fake.calls == []
    ok = client.post("/api/feedback", data={"type": "bug", "message": "m", "consent": "true"},
                     files={"screenshot": ("s.png", io.BytesIO(PNG), "image/png")})
    assert ok.status_code == 201 and fake.calls[0][2]["files"]["screenshot"][1] == PNG
    bad = client.post("/api/feedback", data={"type": "bug", "message": "m", "consent": "true"},
                      files={"screenshot": ("s.png", io.BytesIO(b"not an image"), "image/png")})
    assert bad.status_code == 400
    monkeypatch.setattr(feedback, "MAX_IMAGE", 100)
    big = client.post("/api/feedback", data={"type": "bug", "message": "m", "consent": "true"},
                      files={"screenshot": ("s.png", io.BytesIO(PNG + b"0" * 200), "image/png")})
    assert big.status_code == 413


def test_rate_limit_per_user(client, fake):
    login(client, uid="spammer")
    for _ in range(feedback.PER_HOUR):
        assert send(client).status_code == 201
    r = send(client)
    assert r.status_code == 429 and "try again" in r.json()["detail"]
    login(client, uid="someone-else")
    assert send(client).status_code == 201


def test_ticket_service_problems_are_friendly_and_leak_nothing(client, fake):
    login(client)
    for failure in (httpx.ConnectError("connection refused to tickets.internal.example"),
                    httpx.ReadTimeout("timed out"), httpx.Response(500, text=f"boom {TOKEN} {URL}"),
                    httpx.Response(503, json={"detail": "Tickets are not configured"}),
                    httpx.Response(401, json={"detail": "Authentication required"})):
        fake.queue.append(failure)
        r = send(client)
        assert r.status_code == 502 and r.json()["detail"] == feedback.FRIENDLY_DOWN
        assert TOKEN not in r.text and "internal.example" not in r.text
    fake.queue.append(httpx.Response(429, json={"detail": "Too many tickets from this user today"}))
    r = send(client)
    assert r.status_code == 429 and "tomorrow" in r.json()["detail"]
    assert len(feedback._recent["amit"]) == 0                  # failed sends do not use up the hourly allowance


# ── what I've sent ───────────────────────────────────────────────────────────
def test_my_feedback_is_scoped_to_the_session_and_allowlisted(client, fake):
    login(client, uid="sso-amit")
    fake.queue.append(httpx.Response(200, json={"tickets": [
        {"id": "FB-0001", "type": "idea", "title": "Sort by company", "status": "planned", "created_at": "2026-11-20T10:00:00",
         "updated_at": "2026-11-21T10:00:00", "replies": [{"body": "Good idea, we are adding it.", "sent_at": "2026-11-21"}],
         "requester_contact": "amit@example.test", "internal_note": "do not show"}]}))
    r = client.get("/api/feedback/mine", params={"requester_ref": "jshq:u_someone_else"})
    assert r.status_code == 200
    assert fake.calls[0][:2] == ("GET", "/tickets/mine") and fake.calls[0][2]["params"] == {"requester_ref": "jshq:sso-amit"}
    t = r.json()["tickets"][0]
    assert t["title"] == "Sort by company" and t["replies"][0]["body"].startswith("Good idea")
    assert "requester_contact" not in t and "internal_note" not in t


def test_my_feedback_failures_are_friendly(client, fake):
    login(client)
    fake.queue.append(httpx.ConnectError("down"))
    assert client.get("/api/feedback/mine").json()["detail"] == feedback.FRIENDLY_DOWN
    fake.queue.append(httpx.Response(500))
    assert client.get("/api/feedback/mine").status_code == 502


# ── the page ─────────────────────────────────────────────────────────────────
def test_feedback_buttons_are_in_the_drawer_and_hidden_until_enabled():
    m = re.search(r'<div id="help-feedback"[^>]*>', HTML)
    assert m and " hidden" in m.group(0)
    for label in ("I need more help", "Something isn't right", "I have an idea", "See what I've sent"):
        assert label in HTML
    for t in ("help", "bug", "idea", "mine"):
        assert f'data-fb="{t}"' in HTML
    assert "fetch('/api/feedback/status')" in HTML and "f.hidden=!_fbEnabled" in HTML


def test_feedback_script_never_uses_innerhtml_and_keeps_drafts():
    js = HTML[HTML.index("// ── Feedback:"):HTML.index('// restore "open"')]
    assert "innerHTML" not in js and "textContent" in js
    assert "_fbDraft" in js and "/^feedback:(help|bug|idea)$/.test(_helpTopic" in HTML      # a form is never wiped by navigation


def test_feedback_has_its_own_help_topic_and_doc_for_operators():
    assert (ROOT / "static" / "help" / "feedback.html").exists() and "'feedback':'Sending feedback'" in HTML
    readme = (ROOT / "README.md").read_text()
    assert "ATQ_TICKETS_URL" in readme and "ATQ_TICKETS_TOKEN" in readme
    compose = (ROOT / "docker-compose.yaml").read_text()
    assert "ATQ_TICKETS_URL=${ATQ_TICKETS_URL:-}" in compose and "ATQ_TICKETS_TOKEN=${ATQ_TICKETS_TOKEN:-}" in compose
    assert TOKEN not in HTML and TOKEN not in readme and TOKEN not in compose
