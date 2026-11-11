"""Background board search: cancellable scrape process, task lifecycle, and the HTTP endpoints."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

import auth
import jobspy_search
import main
import search_tasks
import search_worker
from jobspy_search import JobSearchRequest, JobSearchResult


def test_scrape_runs_in_child_process_and_returns_rows():
    df = jobspy_search._scrape_cancellable({}, threading.Event(), target=search_worker._return_rows)
    assert list(df["title"]) == ["T"]


def test_cancel_kills_a_stuck_scrape_quickly():
    ev, out = threading.Event(), {}
    t = threading.Thread(target=lambda: out.setdefault("r", jobspy_search._scrape_cancellable({}, ev, target=search_worker._sleep_forever)))
    t.start()
    time.sleep(1.0)
    started = time.time()
    ev.set()
    t.join(15)
    assert not t.is_alive() and out["r"] is None
    assert time.time() - started < 10


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_task_lifecycle_cancel_isolation_and_cap():
    gate = threading.Event()

    def runner(req, uid, cancel):
        while not gate.is_set() and not cancel.is_set():
            time.sleep(0.02)
        return JobSearchResult(cancelled=cancel.is_set(), created=[{"id": "x"}] if not cancel.is_set() else [])

    req = JobSearchRequest(search_term="x", sites=["indeed"])
    t1 = search_tasks.start(req, "alice", runner)
    assert search_tasks.status(t1, "alice")["state"] == "running"
    assert search_tasks.status(t1, "bob") is None                      # other users can't see it
    assert search_tasks.cancel("bob") == 0                             # ...or cancel it
    assert search_tasks.cancel("alice", [t1]) == 1
    st = wait_for(lambda: (s := search_tasks.status(t1, "alice"))["state"] != "running" and s)
    assert st["state"] == "cancelled" and st["result"]["cancelled"] is True

    t2 = search_tasks.start(req, "alice", runner)
    gate.set()
    st = wait_for(lambda: (s := search_tasks.status(t2, "alice"))["state"] != "running" and s)
    assert st["state"] == "done" and st["result"]["created"] == [{"id": "x"}]

    gate.clear()
    ids = [search_tasks.start(req, "carol", runner) for _ in range(search_tasks.MAX_ACTIVE_PER_USER)]
    with pytest.raises(search_tasks.TooManySearches):
        search_tasks.start(req, "carol", runner)
    search_tasks.cancel("carol")
    for i in ids:
        wait_for(lambda i=i: search_tasks.status(i, "carol")["state"] != "running")


def test_runner_exception_becomes_error_state():
    def boom(req, uid, cancel):
        raise RuntimeError("blocked by board")
    tid = search_tasks.start(JobSearchRequest(search_term="x"), "dave", boom)
    st = wait_for(lambda: (s := search_tasks.status(tid, "dave"))["state"] != "running" and s)
    assert st["state"] == "error" and "blocked by board" in st["error"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "is_setup_complete", lambda: True)
    monkeypatch.setattr(auth, "get_session_secret", lambda: "synthetic-session-secret")
    c = TestClient(main.app)
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    r = Response()
    auth.set_session_cookie(r, "erin", "e@example.test", "Erin")
    c.cookies.set(auth.COOKIE_NAME, SimpleCookie(r.headers["set-cookie"])[auth.COOKIE_NAME].value)
    return c


def test_http_start_poll_cancel(client, monkeypatch):
    def slow(req, uid, cancel):
        while not cancel.is_set():
            time.sleep(0.02)
        return JobSearchResult(cancelled=True)
    monkeypatch.setattr(jobspy_search, "run_search", slow)
    tid = client.post("/api/search/start", json={"search_term": "pm", "sites": ["indeed"]}).json()["task_id"]
    assert client.get(f"/api/search/status/{tid}").json()["state"] == "running"
    assert client.post("/api/search/cancel", json={"task_ids": [tid]}).json() == {"cancelled": 1}
    wait_for(lambda: client.get(f"/api/search/status/{tid}").json()["state"] == "cancelled")
    assert client.get("/api/search/status/nope").status_code == 404
