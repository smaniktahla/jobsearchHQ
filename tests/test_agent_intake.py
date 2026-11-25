from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import auth
import main
import storage
from models import AgentEventType, Job, JobStatus, ReviewStatus
from scoring import extract_job_metadata


ADMIN = "test-admin"
TEST_KEY = "test-agent-key"
URL = "https://jobs.example.test/posting/42"
POSTING = {
    "source": "manual_paste",
    "url": URL,
    "raw_input": "Build and maintain data pipelines.",
    "title": "Data Engineer",
    "company": "Example Company",
    "apply_url": "https://jobs.example.test/apply/42",
    "requisition_id": "REQ-42",
    "origin": "manual-cohort",
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(auth, "SYSTEM_CONFIG_PATH", storage.SYSTEM_CONFIG_PATH)
    auth.save_system_config({"admin_user_id": ADMIN, "agent_api_key": TEST_KEY})
    metadata = Mock(return_value={"pay_range": "Example pay range"})
    monkeypatch.setattr(main.scoring, "extract_job_metadata", metadata)
    return TestClient(main.app, headers={"X-API-Key": TEST_KEY}), metadata


def snapshot():
    return {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in storage.DATA_DIR.rglob("*.json")
    }


def test_creates_pending_job_with_one_audit_entry(api):
    client, metadata = api
    response = client.post("/api/agent/intake", json=POSTING)

    assert response.status_code == 200
    job = storage.load_job(ADMIN, response.json()["id"])
    assert response.json() == {"created": True, "id": job.id}
    assert job.user_id == ADMIN
    assert job.source == POSTING["source"]
    assert (job.title, job.company, job.url) == (POSTING["title"], POSTING["company"], URL)
    assert job.raw_jd == POSTING["raw_input"]
    assert job.ats_url == POSTING["apply_url"]
    assert job.requisition_id == POSTING["requisition_id"]
    assert job.pay_range == "Example pay range"
    assert job.status == JobStatus.NEW
    assert job.review_status == ReviewStatus.PENDING
    assert not job.ready_to_apply
    assert job.score is None
    assert not job.tailored_resume and not job.tailored_resume_docx and not job.cover_letters
    event, = job.agent_log
    assert event.event_type == AgentEventType.INTAKE
    assert event.notes == "intake via agent api (manual-cohort)"
    metadata.assert_called_once_with(POSTING["raw_input"], ADMIN, initialize_config=False)


@pytest.mark.parametrize("match", ["url", "title_company", "archived", "unindexed_archive"])
def test_duplicate_returns_existing_state_without_writes(api, monkeypatch, match):
    client, metadata = api
    existing = Job(
        url=URL + "/" if match != "title_company" else "https://jobs.example.test/other",
        title=" data engineer " if match == "title_company" else "Other title",
        company="example company" if match == "title_company" else "Other company",
        status=JobStatus.PASSED,
        review_status=ReviewStatus.REJECTED,
    )
    storage.save_job(ADMIN, existing)
    if match in ("archived", "unindexed_archive"):
        assert storage.archive_job(ADMIN, existing.id)
        assert storage.load_dedup_index(ADMIN)[0]["archived"]
    if match == "unindexed_archive":
        (storage.get_user_dir(ADMIN) / "dedup_index.json").unlink()
    before = snapshot()
    parse = Mock(side_effect=AssertionError("duplicate reached intake parser"))
    monkeypatch.setattr(main, "process_intake", parse)

    response = client.post("/api/agent/intake", json=POSTING)

    assert response.status_code == 200
    assert response.json() == {
        "created": False, "id": existing.id, "reason": "duplicate",
        "status": "passed", "review_status": "rejected",
    }
    assert snapshot() == before
    parse.assert_not_called()
    metadata.assert_not_called()


@pytest.mark.parametrize("payload", [
    POSTING,
    {"raw_input": POSTING["raw_input"], "origin": "test"},
    {"url": URL, "origin": "test"},
])
def test_repeat_creates_one_job(api, payload):
    client, metadata = api
    first = client.post("/api/agent/intake", json=payload)
    assert first.status_code == 200 and first.json()["created"] is True
    before = snapshot()
    second = client.post("/api/agent/intake", json=payload)

    assert second.status_code == 200
    assert second.json() == {
        "created": False, "id": first.json()["id"], "reason": "duplicate",
        "status": "new", "review_status": "pending",
    }
    assert len(storage.load_all_jobs(ADMIN)) == 1
    assert snapshot() == before
    assert metadata.call_count == int(bool(payload.get("raw_input")))


def test_duplicate_from_extracted_metadata_does_not_initialize_config(api, monkeypatch):
    client, _ = api
    existing = Job(title=POSTING["title"], company=POSTING["company"])
    storage.save_job(ADMIN, existing)
    monkeypatch.setattr(main.scoring, "extract_job_metadata", extract_job_metadata)
    monkeypatch.setattr(main.ai_router, "chat", lambda *args, **kwargs: (
        '{"title": "Data Engineer", "company": "Example Company"}'
    ))
    before = snapshot()

    response = client.post("/api/agent/intake", json={"raw_input": POSTING["raw_input"]})

    assert response.status_code == 200
    assert response.json()["id"] == existing.id
    assert response.json()["created"] is False
    assert snapshot() == before


def test_bad_key_returns_401(api):
    client, _ = api
    response = client.post("/api/agent/intake", json=POSTING, headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_missing_url_and_description_returns_400(api):
    client, _ = api
    response = client.post("/api/agent/intake", json={**POSTING, "url": " ", "raw_input": None})
    assert response.status_code == 400
