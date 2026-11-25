"""User score override + score history: the model's scores are always kept; the user's score wins everywhere."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import archiving
import auth
import auto_approve
import main
import storage
from models import (CoverLetter, Job, JobStatus, MarketLane, ReviewStatus, ScoreBreakdown)


def breakdown(total=6, lane=MarketLane.CONTRACT, model="gemini/gemini-test"):
    sm, si, pa = min(total, 4), min(max(total - 4, 0), 3), min(max(total - 7, 0), 2)
    return ScoreBreakdown(skills_match=sm, scope_impact=si, pay_alignment=pa, total=total,
                          recommended_lane=lane, recommended_resume="base", model=model)


def scored_job(total=6, **kw):
    j = Job(title="Director", company="Acme", raw_jd="x" * 300, **kw)
    j.set_model_score(breakdown(total))
    return j


# ── history ──────────────────────────────────────────────────────────────────
def test_every_model_score_is_kept_in_history():
    j = scored_job(6)
    j.set_model_score(breakdown(8, model="gemini/gemini-other"))
    assert [e.total for e in j.score_history] == [6, 8]
    assert all(e.source == "model" for e in j.score_history)
    assert j.score.total == 8 and j.score_history[-1].model == "gemini/gemini-other"


def test_legacy_score_is_seeded_into_history_before_first_change():
    j = Job(title="Old", company="Co", score=breakdown(5))      # scored before history existed
    assert j.score_history == []
    j.set_model_score(breakdown(7))
    assert [e.total for e in j.score_history] == [5, 7]
    assert "before history" in j.score_history[0].note


# ── override ─────────────────────────────────────────────────────────────────
def test_override_wins_but_model_score_is_untouched():
    j = scored_job(4)
    j.set_user_score(9, "this is exactly my background")
    assert j.effective_total == 9 and j.score_is_user and j.score.total == 4
    assert j.score_history[-1].source == "user" and j.score_history[-1].note.startswith("this is")


def test_rescoring_keeps_the_override_and_records_the_new_model_score():
    j = scored_job(4)
    j.set_user_score(9)
    j.set_model_score(breakdown(3))
    assert j.effective_total == 9 and j.score.total == 3
    assert [e.source for e in j.score_history] == ["model", "user", "model"]


def test_clearing_goes_back_to_the_model_score_and_is_recorded():
    j = scored_job(4)
    j.set_user_score(9)
    j.clear_user_score()
    assert j.effective_total == 4 and not j.score_is_user
    assert j.score_history[-1].source == "user_cleared"
    n = len(j.score_history)
    j.clear_user_score()                       # nothing to clear: no new entry
    assert len(j.score_history) == n


def test_unscored_job_has_no_effective_total():
    assert Job(title="t", company="c").effective_total is None


# ── decisions use the user's score ───────────────────────────────────────────
def _ready(j, tmp_path):
    docx = tmp_path / "resume.docx"; docx.write_text("x")
    j.review_status = ReviewStatus.APPROVED
    j.url = "https://jobs.example.test/posting/1"
    j.posting_status = "open"
    j.tailored_resume_docx = str(docx)
    letter = tmp_path / "letter.docx"; letter.write_text("x")
    j.cover_letters = [CoverLetter(job_id=j.id, variant="direct", content="hi", docx_path=str(letter))]
    return j


def test_ready_to_apply_follows_the_users_score_and_lifts_the_ai_ignore_lane(tmp_path):
    j = _ready(scored_job(3, market_lane=MarketLane.IGNORE), tmp_path)
    assert not j.ready_to_apply                      # AI: low score + ignore
    j.set_user_score(8)
    assert j.ready_to_apply                          # user disagrees: the ignore lane no longer blocks
    j.set_user_score(2)
    assert not j.ready_to_apply                      # user rates it low: blocked by their score


def test_archiving_uses_the_users_score():
    cutoff = datetime.now(timezone.utc)
    old = (cutoff - timedelta(days=30)).isoformat()
    high_but_user_low = scored_job(9); high_but_user_low.updated_at = old
    high_but_user_low.set_user_score(2)
    assert archiving.is_archivable(high_but_user_low, cutoff, 8)
    low_but_user_high = scored_job(3); low_but_user_high.updated_at = old
    low_but_user_high.set_user_score(9)
    assert not archiving.is_archivable(low_but_user_high, cutoff, 8)


def test_auto_approve_uses_the_users_score(tmp_path):
    docx = tmp_path / "r.docx"; docx.write_text("x")
    def eligible(total, user=None):
        j = scored_job(total); j.status = JobStatus.SCORED
        j.tailored_resume_docx = str(docx)
        j.cover_letters = [CoverLetter(job_id=j.id, variant="direct", content="hi")]
        if user is not None:
            j.set_user_score(user)
        return auto_approve.is_eligible(j, 8)
    assert eligible(9) and not eligible(9, user=3)
    assert not eligible(4) and eligible(4, user=9)


# ── API ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "is_setup_complete", lambda: True)
    monkeypatch.setattr(auth, "get_session_secret", lambda: "synthetic-session-secret")
    monkeypatch.setattr(main.scheduler, "update_schedule", lambda *a: None)
    return TestClient(main.app)


def login(client, uid):
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    r = Response()
    auth.set_session_cookie(r, uid, f"{uid}@example.test", uid)
    client.cookies.set(auth.COOKIE_NAME, SimpleCookie(r.headers["set-cookie"])[auth.COOKIE_NAME].value)


def test_override_api_round_trip(client):
    login(client, "amit")
    j = scored_job(4)
    storage.save_job("amit", j)

    r = client.put(f"/api/jobs/{j.id}/score-override", json={"total": 9, "note": "great fit"})
    assert r.status_code == 200
    body = r.json()
    assert body["effective_total"] == 9 and body["score_is_user"] is True and body["score"]["total"] == 4
    assert body["score_override"]["note"] == "great fit"
    assert [e["source"] for e in body["score_history"]] == ["model", "user"]

    # list filters and rows use the user's score; slim rows stay small (no history)
    rows = client.get("/api/jobs?slim=true&min_score=8").json()
    assert [x["id"] for x in rows] == [j.id] and rows[0]["effective_total"] == 9
    assert "score_history" not in rows[0]

    r = client.delete(f"/api/jobs/{j.id}/score-override")
    assert r.status_code == 200 and r.json()["effective_total"] == 4 and r.json()["score_is_user"] is False
    assert [e["source"] for e in r.json()["score_history"]] == ["model", "user", "user_cleared"]
    assert client.get("/api/jobs?slim=true&min_score=8").json() == []


def test_override_api_validates_and_is_per_user(client):
    login(client, "amit")
    j = scored_job(4)
    storage.save_job("amit", j)
    assert client.put(f"/api/jobs/{j.id}/score-override", json={"total": 11}).status_code == 400
    assert client.put(f"/api/jobs/{j.id}/score-override", json={"total": -1}).status_code == 400
    assert client.put("/api/jobs/nope/score-override", json={"total": 5}).status_code == 404
    login(client, "someone-else")
    assert client.put(f"/api/jobs/{j.id}/score-override", json={"total": 5}).status_code == 404
