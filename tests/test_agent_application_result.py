from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

import main
import storage
from models import AgentEvent, CoverLetter, Job, JobStatus, ReviewStatus


USER_ID = "test-admin"
HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture
def agent_job(monkeypatch):
    monkeypatch.setattr(main, "verify_agent_api_key", lambda key: key == HEADERS["X-API-Key"])
    monkeypatch.setattr(main, "get_admin_user_id", lambda: USER_ID)
    job = Job(status=JobStatus.SCORED, posting_status="open")
    storage.save_job(USER_ID, job)
    return job, f"/api/agent/jobs/{job.id}"


def test_confirmed_result_replay_and_conflict(agent_job):
    job, path = agent_job
    client = TestClient(main.app, headers=HEADERS)
    payload = {
        "run_id": "run-1",
        "idempotency_key": "attempt-1",
        "outcome": "confirmed",
        "notes": "Submission recorded",
        "portal_url": "https://example.test/apply",
        "applied_at": "2025-01-01",
        "evidence": {"title": "Confirmation", "text_sha256": "a" * 64},
    }
    first = client.post(f"{path}/application-result", json=payload)
    assert first.status_code == 200
    assert first.json()["status"] == "applied"
    assert first.json()["applied_at"] == "2025-01-01T00:00:00+00:00"
    stored = storage.load_job(USER_ID, job.id)
    assert len(stored.status_history) == len(stored.agent_log) == 1
    assert stored.notes == payload["notes"]
    assert stored.follow_up.notes == f"Portal: {payload['portal_url']}"
    event = stored.agent_log[0]
    assert event.run_id == payload["run_id"]
    assert event.idempotency_key == payload["idempotency_key"]
    assert event.actor == "agent"
    assert event.evidence.text_sha256 == "a" * 64

    job_file = storage.get_user_dir(USER_ID) / "jobs" / f"{job.id}.json"
    before = job_file.stat()
    replay = client.post(f"{path}/application-result", json={**payload, "notes": "Ignored"})
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert job_file.stat().st_ino == before.st_ino
    assert job_file.stat().st_mtime_ns == before.st_mtime_ns

    conflict = client.post(f"{path}/application-result", json={**payload, "run_id": "run-2"})
    assert conflict.status_code == 409
    after = storage.load_job(USER_ID, job.id)
    rejected = after.agent_log.pop()
    assert rejected.run_id == "run-2"
    assert rejected.success is False
    assert after == stored


def test_unconfirmed_remains_pending_until_explicit_confirmation(agent_job, tmp_path):
    job, path = agent_job
    resume = tmp_path / "resume.docx"
    resume.touch()
    job.tailored_resume_docx = str(resume)
    job.cover_letters = [CoverLetter(job_id=job.id, variant="direct", content="Test")]
    job.review_status = ReviewStatus.APPROVED
    job.set_user_score(8)
    storage.save_job(USER_ID, job)
    assert job.ready_to_apply

    client = TestClient(main.app, headers=HEADERS)
    url = f"{path}/application-result"
    payload = {"run_id": "run-1", "outcome": "unconfirmed", "evidence": {"note": "No receipt"}}
    first = client.post(url, json=payload)
    assert first.status_code == 200
    body = first.json()
    marker = body["submission_unconfirmed"]
    assert body["status"] == "scored"
    assert body["status_history"] == []
    assert body["applied_at"] == body["follow_up"]["due_at"] == ""
    assert body["ready_to_apply"] is False
    assert marker == {
        "run_id": "run-1",
        "at": body["agent_log"][0]["at"],
        "evidence_ref": body["agent_log"][0]["id"],
    }
    assert client.get("/api/agent/jobs").json()["jobs"][0]["submission_unconfirmed"] == marker
    assert client.post(url, json=payload).json() == body
    assert client.post(url, json={"run_id": "run-1"}).json() == body

    confirmation = client.post(url, json={**payload, "outcome": "confirmed"})
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "applied"
    assert confirmation.json()["submission_unconfirmed"] is None
    assert client.post(url, json=payload).json() == confirmation.json()
    stored = storage.load_job(USER_ID, job.id)
    assert len(stored.status_history) == 1
    assert [event.success for event in stored.agent_log] == [None, True]


@pytest.mark.parametrize("kind", ["closed_posting", "accessibility_preflight"])
def test_evidence_appends_to_log_only(agent_job, kind):
    job, path = agent_job
    client = TestClient(main.app, headers=HEADERS)
    payload = {
        "kind": kind, "verdict": "blocked", "url": "https://example.test/apply",
        "observed_at": "2025-01-01T12:30:00Z", "note": "Synthetic observation",
    }
    assert TestClient(main.app).post(f"{path}/evidence", json=payload).status_code == 401
    assert storage.archive_job(USER_ID, job.id)
    before = storage.load_job(USER_ID, job.id)
    response = client.post(f"{path}/evidence", json=payload)
    assert response.status_code == 200
    assert response.json()["event"]["evidence"] == payload
    after = storage.load_job(USER_ID, job.id)
    assert after.agent_log.pop().evidence.model_dump() == payload
    assert after == before
    assert storage.load_all_jobs(USER_ID) == []
    assert storage.load_dedup_index(USER_ID)[0]["archived"] is True


@pytest.mark.parametrize("different_runs", [False, True])
def test_concurrent_results_commit_one_transition(agent_job, different_runs):
    job, path = agent_job
    start = Barrier(2)

    def submit(run_id):
        client = TestClient(main.app, headers=HEADERS)
        start.wait(timeout=10)
        return client.post(f"{path}/application-result", json={"run_id": run_id, "notes": "Once"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["run-1", "run-2" if different_runs else "run-1"]))
    assert sorted(r.status_code for r in responses) == ([200, 409] if different_runs else [200, 200])
    stored = storage.load_job(USER_ID, job.id)
    assert stored.status == JobStatus.APPLIED
    assert stored.notes == "Once"
    assert len(stored.status_history) == 1
    assert sum(event.success is True for event in stored.agent_log) == 1
    assert len(stored.agent_log) == (2 if different_runs else 1)


@pytest.mark.parametrize("field,length", [
    ("url", 2049), ("title", 513), ("text_sha256", 65),
    ("screenshot_sha256", 65), ("report_hash", 129), ("note", 2049),
])
def test_application_evidence_bounds(agent_job, field, length):
    job, path = agent_job
    client = TestClient(main.app, headers=HEADERS)
    response = client.post(f"{path}/application-result", json={"evidence": {field: "x" * length}})
    assert response.status_code == 422
    assert storage.load_job(USER_ID, job.id).agent_log == []


def test_legacy_result_and_event(agent_job):
    job, path = agent_job
    client = TestClient(main.app, headers=HEADERS)
    assert AgentEvent(event_type="submit").run_id is None
    response = client.post(f"{path}/application-result", json={"status": "passed", "error": "Test"})
    assert response.status_code == 200
    assert response.json()["status"] == "passed"
    assert response.json()["notes"] == "Automation error: Test"


def test_atomic_save_preserves_previous_job_on_replace_failure(agent_job, monkeypatch):
    job, _ = agent_job
    directory = storage.get_user_dir(USER_ID) / "jobs"
    path = directory / f"{job.id}.json"
    before = path.read_bytes()

    def fail_replace(source, destination):
        assert source.parent == destination.parent == directory
        assert destination.read_bytes() == before
        assert '"notes": "Pending edit"' in source.read_text()
        raise OSError("simulated replacement failure")

    job.notes = "Pending edit"
    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replacement failure"):
        storage.save_job(USER_ID, job)
    assert path.read_bytes() == before
    assert list(directory.iterdir()) == [path]
