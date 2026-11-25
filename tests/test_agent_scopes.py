import json
from datetime import datetime
from http.cookies import SimpleCookie

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

import auth
import main
import storage
from models import AgentEvent, AgentEventType, CoverLetter, Job, ReviewStatus, ScoreBreakdown


ADMIN = "scope-test-admin"
MANAGEMENT = "synthetic-management"
EXECUTOR = "synthetic-executor"
KEY_PATH = "/api/agent/keys/executor"
JOB_PATH = "/api/agent/jobs/{job_id}"
SCORE = ScoreBreakdown(skills_match=4, scope_impact=3, pay_alignment=1, total=8)
EVIDENCE = {
    "kind": "closed_posting",
    "verdict": "not_found",
    "url": "https://jobs.example.test/example",
    "observed_at": "2026-01-01T00:00:00+00:00",
}

# Explicit method/path inventory. A new route must make a permission decision here.
ROUTES = {
    ("GET", "/api/agent/key"): ("admin_session", None),
    ("GET", KEY_PATH): ("admin_session", None),
    ("POST", KEY_PATH): ("admin_session", None),
    ("GET", "/api/agent/ready-queue"): ("executor_read", None),
    ("GET", "/api/agent/jobs"): ("executor_read", None),
    ("GET", "/api/agent/profile"): ("executor_read", None),
    ("PUT", "/api/agent/profile"): ("management", {"name": "Synthetic"}),
    ("GET", "/api/agent/writing-samples"): ("executor_read", None),
    ("POST", "/api/agent/intake"): ("management", {"raw_input": "Example job description"}),
    ("GET", JOB_PATH + "/package"): ("executor_read", None),
    ("GET", JOB_PATH + "/research"): ("management", None),
    ("POST", JOB_PATH + "/research"): ("management", {}),
    ("GET", JOB_PATH + "/resume"): ("executor_read", None),
    ("GET", JOB_PATH + "/resume/download"): ("executor_read", None),
    ("GET", JOB_PATH + "/cover-letter/download"): ("executor_read", None),
    ("PATCH", JOB_PATH + "/ats-url"): ("management", {"ats_url": EVIDENCE["url"]}),
    ("POST", JOB_PATH + "/log"): ("executor_write", {"event_type": "dispatch"}),
    ("GET", JOB_PATH + "/log"): ("executor_read", None),
    ("POST", JOB_PATH + "/application-result"): ("executor_write", {"status": "applied"}),
    ("POST", JOB_PATH + "/evidence"): ("executor_write", EVIDENCE),
    ("GET", JOB_PATH + "/eligibility"): ("executor_read", None),
    ("PATCH", JOB_PATH + "/review"): ("management", {"review_status": "approved"}),
    ("GET", "/api/agent/jobs/needs-refresh"): ("management", None),
    ("POST", JOB_PATH + "/refresh-and-score"): ("management", {"raw_jd": "Updated example"}),
}
REGISTERED_ROUTES = {
    (method, route.path)
    for route in main.app.routes
    if route.path.startswith("/api/agent/")
    for method in route.methods
}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "SYSTEM_CONFIG_PATH", tmp_path / "system_config.json")
    auth.save_system_config({
        "admin_user_id": ADMIN,
        "agent_api_key": MANAGEMENT,
        "executor_api_key": EXECUTOR,
    })
    document = tmp_path / "sample.docx"
    document.write_bytes(b"synthetic document")
    job = Job(
        id="synthetic",
        title="Engineer",
        company="Example",
        score=SCORE,
        resume_lane_override="base",
        tailored_resume_docx=str(document),
        cover_letters=[
            CoverLetter(job_id="synthetic", variant="direct", content="Synthetic", docx_path=str(document)),
        ],
    )
    storage.save_job(ADMIN, job)
    monkeypatch.setattr(main.company_research, "research_company", lambda **kwargs: {})
    monkeypatch.setattr(main.scoring, "score_job", lambda *args: SCORE)
    monkeypatch.setattr(main.scoring, "extract_job_metadata", lambda *args, **kwargs: {})
    return TestClient(main.app), job


def headers(key):
    return {"X-API-Key": key} if key else {}


def signed_session(client, user_id):
    response = Response()
    auth.set_session_cookie(response, user_id, "", "")
    cookies = SimpleCookie(response.headers["set-cookie"])
    client.cookies.set(auth.COOKIE_NAME, cookies[auth.COOKIE_NAME].value)


def test_agent_route_inventory():
    assert REGISTERED_ROUTES == ROUTES.keys()


@pytest.mark.parametrize(("method", "route"), sorted(REGISTERED_ROUTES))
def test_agent_route_key_matrix(api, monkeypatch, method, route):
    client, job = api
    scope, body = ROUTES[method, route]
    path = route.replace("{job_id}", job.id)
    checked_scopes = []
    check_scope = auth.require_agent_scope

    def track_scope(request, required_scope):
        checked_scopes.append(required_scope)
        return check_scope(request, required_scope)

    monkeypatch.setattr(auth, "require_agent_scope", track_scope)

    def call(key):
        # A caller-supplied scope must have no effect on the route's policy.
        return client.request(method, path, headers=headers(key), json=body, params={"scope": "executor_read"})

    assert call(None).status_code == 401
    assert call("unknown").status_code == 401
    expected = {"admin_session": 401, "management": 403}.get(scope, 200)
    assert call(EXECUTOR).status_code == expected
    assert call(MANAGEMENT).status_code == (401 if scope == "admin_session" else 200)
    assert checked_scopes == ([] if scope == "admin_session" else [scope] * 4)

    audit_path = auth.DATA_DIR / "agent_scope_audit.jsonl"
    if scope == "management":
        line, = audit_path.read_text().splitlines()
        event = AgentEvent.model_validate_json(line)
        assert event.event_type == AgentEventType.SCOPE_DENIED
        assert event.success is False
        assert f"{method} {route}" in event.notes
        assert EXECUTOR not in line and MANAGEMENT not in line and job.id not in line
    else:
        assert not audit_path.exists()


def test_executor_key_generation_and_rotation_requires_admin_session(api):
    client, _ = api
    with auth.edit_system_config() as config:
        del config["executor_api_key"]

    signed_session(client, "other-user")
    assert client.get(KEY_PATH).status_code == 403
    assert client.post(KEY_PATH).status_code == 403
    assert "executor_api_key" not in auth.load_system_config()

    signed_session(client, ADMIN)
    first = client.get(KEY_PATH)
    key = first.json()["executor_api_key"]
    assert first.headers["Cache-Control"] == "no-store"
    assert key == auth.load_system_config()["executor_api_key"]
    assert client.get(KEY_PATH).json() == first.json()
    rotated = client.post(KEY_PATH)
    new_key = rotated.json()["executor_api_key"]
    assert new_key != key and new_key == auth.load_system_config()["executor_api_key"]
    assert rotated.headers["Cache-Control"] == "no-store"
    assert client.get("/api/agent/key").json() == {"agent_api_key": MANAGEMENT}
    client.cookies.clear()
    assert client.get("/api/agent/profile", headers=headers(key)).status_code == 401
    assert client.get("/api/agent/profile", headers=headers(new_key)).status_code == 200


def test_executor_can_filter_jobs_but_cannot_approve(api):
    client, job = api
    assert client.get("/api/agent/jobs", headers=headers(EXECUTOR), params={
        "status": "new", "company": "examp", "q": "engineer", "lane": "contract",
    }).json()["count"] == 1
    assert client.get("/api/agent/jobs", headers=headers(EXECUTOR), params={"q": "absent"}).json()["count"] == 0
    path = JOB_PATH.format(job_id=job.id) + "/review"
    assert client.patch(path, headers=headers(EXECUTOR), json={"review_status": "approved"}).status_code == 403
    assert storage.load_job(ADMIN, job.id).review_status == ReviewStatus.PENDING
    assert client.patch(path, headers=headers(MANAGEMENT), json={"review_status": "approved"}).status_code == 200
    approved = storage.load_job(ADMIN, job.id)
    assert approved.approved_by == "agent-management"
    assert datetime.fromisoformat(approved.approved_at).utcoffset() is not None
    assert client.patch(path, headers=headers(MANAGEMENT), json={"review_status": "rejected"}).status_code == 200
    rejected = storage.load_job(ADMIN, job.id)
    assert rejected.approved_by is None and rejected.approved_at == ""


def test_executor_activity_preserves_archive(api):
    client, job = api
    assert storage.archive_job(ADMIN, job.id)
    path = JOB_PATH.format(job_id=job.id)
    assert client.post(path + "/log", headers=headers(EXECUTOR), json={"event_type": "dispatch"}).status_code == 200
    for kind in ("closed_posting", "accessibility_preflight"):
        assert client.post(path + "/evidence", headers=headers(EXECUTOR), json={
            **EVIDENCE, "kind": kind,
        }).status_code == 200
    assert client.post(path + "/application-result", headers=headers(EXECUTOR), json={"status": "applied"}).status_code == 200
    assert storage.load_all_jobs(ADMIN) == []
    archived, = storage.load_archived_jobs(ADMIN)
    assert [event.evidence.kind for event in archived.agent_log if event.evidence] == [
        "closed_posting", "accessibility_preflight",
    ]
    assert storage.load_dedup_index(ADMIN)[0]["archived"]


def test_executor_cannot_send_scored_digest(api):
    client, _ = api
    response = client.post("/api/jobs/send-scored-digest", headers=headers(EXECUTOR))
    assert response.status_code == 403
    audit = auth.DATA_DIR / "agent_scope_audit.jsonl"
    assert json.loads(audit.read_text())["event_type"] == "scope_denied"
