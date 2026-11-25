from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import auto_approve
import backfill
import main
import storage
from models import AppConfig, CoverLetter, Job, JobStatus, MarketLane, ReviewStatus, ScoreBreakdown, User

USER_ID = "queue-test"
AGENT_HEADERS = {"X-API-Key": "synthetic-agent-key"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "get_admin_user_id", lambda: USER_ID)
    monkeypatch.setattr(main, "verify_agent_api_key", lambda key: key == AGENT_HEADERS["X-API-Key"])
    monkeypatch.setattr(auth, "get_digest_action_secret", lambda: "synthetic-digest-secret")
    monkeypatch.setitem(main.app.dependency_overrides, main.get_current_user, lambda: User(id=USER_ID))
    return TestClient(main.app)


def ready_job(tmp_path, posting="1", score=8):
    resume = tmp_path / f"resume-{posting}.docx"
    letter = tmp_path / f"letter-{posting}.docx"
    resume.write_bytes(b"test resume")
    letter.write_bytes(b"test cover letter")
    job = Job(
        title="Example role", company="Example company", raw_jd="Example description",
        status=JobStatus.SCORED, review_status=ReviewStatus.APPROVED, posting_status="open",
        score=ScoreBreakdown(skills_match=4, scope_impact=3, pay_alignment=1, total=score),
        url=f"https://jobs.example.test/postings/{posting}", tailored_resume_docx=str(resume),
    )
    job.cover_letters = [CoverLetter(job_id=job.id, variant="direct", content="Example", docx_path=str(letter))]
    return job


def eligibility(client, job_id):
    return client.get(f"/api/agent/jobs/{job_id}/eligibility", headers=AGENT_HEADERS)


def queues(client):
    return (
        client.get("/api/agent/ready-queue", headers=AGENT_HEADERS),
        client.get("/api/jobs/apply-queue"),
    )


@pytest.mark.parametrize(("changes", "reason"), [
    ({"review_status": ReviewStatus.PENDING}, "not_approved"),
    ({"review_status": ReviewStatus.REJECTED}, "not_approved"),
    ({"score": None}, "not_scored"),
    ({"market_lane": MarketLane.IGNORE}, "ignored_lane"),
    ({"posting_status": "CLOSED"}, "posting_not_open"),
    ({"posting_status": ""}, "posting_not_open"),
    ({"posting_status": "unknown"}, "posting_not_open"),
    ({"applied_at": "2026-01-01"}, "already_actioned"),
    ({"status": JobStatus.APPLIED}, "already_actioned"),
    ({"status": JobStatus.INTERVIEW}, "already_actioned"),
    ({"status": JobStatus.OFFER}, "already_actioned"),
    ({"status": JobStatus.REJECTED}, "already_actioned"),
    ({"status": JobStatus.PASSED}, "already_actioned"),
    ({"status_history": [{"to": "passed"}, {"to": "scored"}]}, "already_actioned"),
    ({"tailored_resume_docx": ""}, "missing_tailored_resume"),
    ({"cover_letters": []}, "missing_cover_letter"),
    ({"url": ""}, "missing_apply_url"),
    ({"url": "javascript:invalid", "ats_url": "https://"}, "missing_apply_url"),
    ({"url": "https://jobs.example.test:bad/apply", "ats_url": "https://jobs.example.test/a b"}, "missing_apply_url"),
])
def test_missing_prerequisites(client, tmp_path, changes, reason):
    job = ready_job(tmp_path).model_copy(update=changes)
    storage.save_job(USER_ID, job)

    assert not job.ready_to_apply
    assert eligibility(client, job.id).json() == {"eligible": False, "reasons": [reason]}
    for response in queues(client):
        assert response.status_code == 200
        assert response.json() == {"count": 0, "jobs": []}


def test_recheck_detects_deleted_files_and_reports_all_blockers(client, tmp_path):
    job = ready_job(tmp_path)
    storage.save_job(USER_ID, job)
    response = eligibility(client, job.id)
    assert response.json() == {"eligible": True, "reasons": []}
    assert response.headers["Cache-Control"] == "no-store"
    assert all(response.json()["count"] == 1 for response in queues(client))

    Path(job.tailored_resume_docx).unlink()
    letter_path = Path(job.cover_letters[0].docx_path)
    letter_path.unlink()
    letter_path.mkdir()
    assert eligibility(client, job.id).json() == {
        "eligible": False, "reasons": ["missing_tailored_resume", "missing_cover_letter"],
    }
    assert all(response.json()["count"] == 0 for response in queues(client))
    assert [found.id for found in backfill.find_missing(USER_ID, 8)] == [job.id]


def test_both_queues_sort_by_effective_score_and_expose_posting_info(client, tmp_path):
    promoted = ready_job(tmp_path, "promoted", score=2)
    promoted.set_user_score(10)
    promoted.ats_url = "https://apply.example.test/form?requisition_id=REQ-123"
    middle = ready_job(tmp_path, "middle", score=8)
    middle.ats_url = "invalid"
    demoted = ready_job(tmp_path, "demoted", score=9)
    demoted.set_user_score(6)
    demoted.raw_jd = "Example posting\nJob ID: ABC-456\n"
    below_minimum = ready_job(tmp_path, "below-minimum", score=4)
    for job in (promoted, middle, demoted, below_minimum):
        storage.save_job(USER_ID, job)

    for response in queues(client):
        rows = response.json()["jobs"]
        assert [row["id"] for row in rows] == [promoted.id, middle.id, demoted.id]
        assert [row["effective_total"] for row in rows] == [10, 8, 6]
        assert [row["apply_url"] for row in rows] == [promoted.ats_url, middle.url, demoted.url]
        assert [row["posting_identity"] for row in rows] == ["REQ-123", "", "ABC-456"]
    assert eligibility(client, below_minimum.id).json()["reasons"] == ["score_below_minimum"]


@pytest.mark.parametrize("match", ["id", "url", "requisition"])
def test_archive_blocks_matching_postings_in_both_queues_and_recheck(client, tmp_path, match):
    active = ready_job(tmp_path, "active")
    archived = ready_job(tmp_path, "archived")
    if match == "id":
        archived.id = active.id
    elif match == "url":
        archived.ats_url = active.url + "/?utm_source=example"
    else:
        archived.requisition_id = "ABC-456"
        active.raw_jd = "Requisition ID: abc-456"
    storage.save_job(USER_ID, archived)
    assert storage.archive_job(USER_ID, archived.id)
    assert eligibility(client, archived.id).json()["reasons"] == ["archived_or_duplicate"]

    storage.save_job(USER_ID, active)
    distinct = ready_job(tmp_path, "distinct")
    distinct.requisition_id = "OTHER-123"
    storage.save_job(USER_ID, distinct)

    assert eligibility(client, active.id).json() == {"eligible": False, "reasons": ["archived_or_duplicate"]}
    for response in queues(client):
        assert [job["id"] for job in response.json()["jobs"]] == [distinct.id]


def test_archive_from_another_user_does_not_block(client, tmp_path):
    job = ready_job(tmp_path)
    storage.save_job("other-test-user", job)
    assert storage.archive_job("other-test-user", job.id)
    storage.save_job(USER_ID, job)
    assert eligibility(client, job.id).json() == {"eligible": True, "reasons": []}


def test_eligibility_auth_and_unknown_job(client, monkeypatch):
    route = "/api/agent/jobs/no-such-job/eligibility"
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-API-Key": "wrong"}).status_code == 401
    assert eligibility(client, "no-such-job").status_code == 404
    monkeypatch.setattr(main, "get_admin_user_id", lambda: None)
    assert eligibility(client, "no-such-job").status_code == 503


@pytest.mark.parametrize("source", ["human-session", "digest-link", "auto-approve", "agent-api"])
def test_approval_paths_record_provenance_once_per_transition(client, tmp_path, source):
    job = ready_job(tmp_path, score=10)
    job.review_status = ReviewStatus.PENDING
    storage.save_job(USER_ID, job)
    token = auth.sign_digest_action(USER_ID, job.id, "approve")

    def approve():
        body = {"review_status": "approved"}
        if source == "human-session":
            response = client.patch(f"/api/jobs/{job.id}", json=body)
            assert response.json()["approved_by"] == source
        elif source == "digest-link":
            response = client.post("/api/digest-action", params={"token": token})
        elif source == "agent-api":
            response = client.patch(f"/api/agent/jobs/{job.id}/review", json=body, headers=AGENT_HEADERS)
        else:
            auto_approve.run(USER_ID, AppConfig(auto_approve_enabled=True), notify=False)
            return
        assert response.status_code == 200

    approve()
    approved = storage.load_job(USER_ID, job.id)
    assert approved.review_status == ReviewStatus.APPROVED
    assert approved.approved_by == source
    assert datetime.fromisoformat(approved.approved_at).tzinfo is not None
    event, = approved.agent_log
    assert event.event_type == "resolve"
    assert event.success
    assert event.at == approved.approved_at
    assert source in event.notes
    assert main.enrich_job(approved)["approved_by"] == source

    approve()
    repeated = storage.load_job(USER_ID, job.id)
    assert repeated.approved_at == approved.approved_at
    assert repeated.agent_log == approved.agent_log


def test_legacy_approval_is_readable_without_fabricating_provenance(tmp_path):
    job = ready_job(tmp_path)
    legacy = job.model_dump(exclude={"approved_by", "approved_at"})
    loaded = Job(**legacy)
    assert loaded.ready_to_apply
    assert loaded.approved_by is None
    assert not loaded.approved_at
    assert not loaded.agent_log


def test_explicit_requisition_id_round_trip(client):
    response = client.post("/api/jobs", json={"raw_jd": "Example posting", "requisition_id": "REQ-1"})
    assert response.status_code == 200
    job = response.json()
    assert job["posting_identity"] == "REQ-1"

    updated = client.patch(f"/api/jobs/{job['id']}", json={"requisition_id": "REQ-2"})
    assert updated.status_code == 200
    assert updated.json()["posting_identity"] == "REQ-2"
    assert storage.load_job(USER_ID, job["id"]).requisition_id == "REQ-2"


def test_reapproval_records_new_source_and_preserves_previous_event(client, tmp_path):
    job = ready_job(tmp_path)
    job.review_status = ReviewStatus.PENDING
    job.approve("auto-approve")
    storage.save_job(USER_ID, job)
    route = f"/api/agent/jobs/{job.id}/review"

    assert client.patch(route, json={"review_status": "pending"}, headers=AGENT_HEADERS).status_code == 200
    assert client.patch(route, json={"review_status": "approved"}, headers=AGENT_HEADERS).status_code == 200
    updated = storage.load_job(USER_ID, job.id)
    assert updated.approved_by == "agent-api"
    assert updated.agent_log[0] == job.agent_log[0]
    assert len(updated.agent_log) == 2
    assert updated.agent_log[1].at == updated.approved_at
