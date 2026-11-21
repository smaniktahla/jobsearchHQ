"""Feedback screenshots (phase 2B): vendored capture library, consent, redaction wiring, size handling, JPEG on the server."""
import hashlib
import io
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
VENDOR = ROOT / "static" / "vendor"
JS = HTML[HTML.index("// ── Screenshot (optional)"):HTML.index('// restore "open"')]


# ── vendored library ─────────────────────────────────────────────────────────
def test_capture_library_is_vendored_and_untampered():
    lib = VENDOR / "html2canvas-pro.min.js"
    readme = (VENDOR / "README.md").read_text()
    sha = hashlib.sha256(lib.read_bytes()).hexdigest()
    assert sha in readme, "static/vendor/README.md must record the SHA-256 of the vendored file"
    assert "MIT License" in (VENDOR / "html2canvas-pro.LICENSE.txt").read_text()
    assert b"html2canvas-pro 2.6.3" in lib.read_bytes()[:300]


def test_capture_library_is_local_and_lazy():
    assert "/static/vendor/html2canvas-pro.min.js?v=2.6.3" in JS
    assert not re.search(r"cdnjs|jsdelivr|unpkg|googleapis", HTML), "no third-party scripts"
    assert not re.search(r'<script[^>]+src="https?:', HTML), "no script is loaded from another site"
    assert "html2canvas" not in re.sub(r"// ── Screenshot.*", "", HTML.split("// ── Screenshot")[0]), "the library is only referenced from the on-demand loader"
    assert "fbLoadCapture()" in JS and "s.onload=res" in JS


# ── screenshot widget wiring ─────────────────────────────────────────────────
def test_screenshot_section_offers_capture_paste_and_file():
    for needle in ("Capture this page", "Choose a file", "Ctrl+V", "image/png,image/jpeg", "clipboardData", "createImageBitmap",
                   "f.addEventListener('paste',shot.onPaste)", "shot.node"):
        assert needle in HTML, needle


def test_capture_leaves_the_help_panel_out_of_the_picture():
    assert "ignoreElements:el=>el.id==='help-drawer'||el.id==='help-backdrop'" in JS


def test_hide_parts_paints_black_into_the_pixels_that_are_sent():
    assert "fillStyle='#000'" in JS and "rects.forEach(r=>g.fillRect" in JS
    assert JS.count("rects.forEach(r=>g.fillRect") >= 2            # in the preview AND in compose() used for sending
    assert "function compose(" in JS and "drawImage(orig" in JS
    assert "onpointerdown" in JS and "setPointerCapture" in JS and "touch-action:none" in HTML      # mouse and touch
    assert "Undo last cover" in JS and "Remove screenshot" in JS


def test_consent_is_required_before_a_screenshot_is_sent():
    assert "I am happy to include this screenshot" in JS and "only the people running JSHQ can see it" in JS
    assert "if(shot.has()&&!shot.consented())" in HTML and "fd.append('consent','true')" in HTML
    assert HTML.index("if(shot.has()&&!shot.consented())") < HTML.index("fd.append('screenshot'")
    assert ".fb-consent input{width:auto" in HTML                # the checkbox sits next to its text


def test_large_screenshots_are_shrunk_before_sending():
    assert "FB_SHOT_MAX_W=1600" in JS and "FB_SHOT_MAX_BYTES=4500000" in JS and "FB_SHOT_HARD_BYTES=4900000" in JS
    assert "image/jpeg" in JS and "compose(0.7)" in JS
    assert feedback.MAX_IMAGE == 5 * 1024 * 1024 and 4900000 < feedback.MAX_IMAGE      # the client limits fit under the server's


def test_screenshot_code_never_uses_innerhtml():
    assert "innerHTML" not in JS


def test_help_explains_screenshots_and_privacy():
    text = (ROOT / "static" / "help" / "feedback.html").read_text()
    assert "How do I add a screenshot?" in text and "Is my screenshot private?" in text and "Capture this page" in text


# ── server side: JPEG screenshots ────────────────────────────────────────────
class FakeATQ:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return httpx.Response(201, json={"id": "FB-0009", "status": "proposed"})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ATQ_TICKETS_URL", "http://tickets.internal.example:8700")
    monkeypatch.setenv("ATQ_TICKETS_TOKEN", "t")
    monkeypatch.setattr(auth, "is_setup_complete", lambda: True)
    monkeypatch.setattr(auth, "get_session_secret", lambda: "synthetic-session-secret")
    monkeypatch.setattr(main.scheduler, "update_schedule", lambda *a: None)
    feedback._recent.clear()
    fake = FakeATQ()
    monkeypatch.setattr(feedback, "_atq", fake)
    c = TestClient(main.app)
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    r = Response()
    auth.set_session_cookie(r, "sso-1", "a@example.test", "A")
    c.cookies.set(auth.COOKIE_NAME, SimpleCookie(r.headers["set-cookie"])[auth.COOKIE_NAME].value)
    c.fake = fake
    return c


def test_jpeg_screenshots_are_accepted_and_forwarded(client):
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    r = client.post("/api/feedback", data={"type": "bug", "message": "m", "consent": "true"},
                    files={"screenshot": ("screenshot.jpg", io.BytesIO(jpeg), "image/jpeg")})
    assert r.status_code == 201
    assert client.fake.calls[0][2]["files"]["screenshot"][1] == jpeg
