"""The Tracker shows and filters by RESUME lane (director/base/contract), not the internal market lane."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import main
import storage
from models import Job, ScoreBreakdown

HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()


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


def make_job(title, lane_override=""):
    j = Job(title=title, company="Acme", raw_jd="x" * 300, resume_lane_override=lane_override)
    j.score = ScoreBreakdown(skills_match=3, scope_impact=2, pay_alignment=1, total=6, recommended_resume="base")
    return j


def test_rows_carry_resume_lane_and_can_be_filtered(client):
    login(client)
    a, b = make_job("A", "director"), make_job("B", "contract")
    storage.save_job("amit", a)
    storage.save_job("amit", b)
    rows = {r["title"]: r for r in client.get("/api/jobs?slim=true").json()}
    assert rows["A"]["resume_lane"] == "director" and rows["B"]["resume_lane"] == "contract"
    only = client.get("/api/jobs?slim=true&resume_lane=director").json()
    assert [r["title"] for r in only] == ["A"]


def test_cache_does_not_mix_up_filters(client):
    login(client)
    storage.save_job("amit", make_job("A", "director"))
    assert len(client.get("/api/jobs?slim=true&resume_lane=director").json()) == 1
    assert client.get("/api/jobs?slim=true&resume_lane=contract").json() == []
    assert len(client.get("/api/jobs?slim=true").json()) == 1


def test_tracker_ui_uses_resume_lane():
    assert 'id="filter-lane"' in HTML and "All resume lanes" in HTML
    assert "RESUME LANE" in HTML and "laneBadge(j.resume_lane)" in HTML
    assert "laneBadge(j.market_lane)" not in HTML and 'id="score-lane-badge"' not in HTML
    assert "p.push('resume_lane='+l)" in HTML
