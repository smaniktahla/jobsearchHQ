"""Batch company research for high-scoring jobs.

Same shape as backfill.py: find the jobs that qualify, run them one at a time in a background
thread with progress and a Stop button, and let the daily pipeline run a capped pass. A job is
researched once; a result with no contacts is retried after RETRY_EMPTY_DAYS (searches can fail
or come back empty for reasons that clear up).
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

import ai_router
import company_research
import storage
from models import CompanyResearch, Contact, JobStatus, MarketLane

logger = logging.getLogger(__name__)

_CLOSED = {JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED, JobStatus.PASSED}
RETRY_EMPTY_DAYS = 3
MAX_CONSECUTIVE_FAILURES = 3  # backend down (no credits, SearXNG/Gemma unreachable): stop instead of failing every job

_lock = threading.Lock()
_state: dict[str, dict] = {}
_cancel: dict[str, threading.Event] = {}


def _needs_research(job) -> bool:
    r = job.research
    if not r or not r.researched_at:
        return True
    if r.contacts or r.company_summary:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(r.researched_at)
    except ValueError:
        return True
    return age > timedelta(days=RETRY_EMPTY_DAYS)


def find_unresearched(user_id: str, threshold: int) -> list:
    """Active, scored jobs at/above threshold with a company name and no (usable) research, best score first."""
    found = []
    for job in storage.load_all_jobs(user_id, copy=False):
        if not job.score or job.score.total < threshold:
            continue
        if job.status in _CLOSED or job.market_lane == MarketLane.IGNORE:
            continue
        if not job.company or job.company.lower() in ("nan", "unknown", ""):
            continue
        if not _needs_research(job):
            continue
        found.append(job)
    found.sort(key=lambda j: (-j.score.total, j.created_at))
    return found


def summarize(user_id: str, threshold: int) -> dict:
    jobs = find_unresearched(user_id, threshold)
    return {
        "threshold": threshold,
        "count": len(jobs),
        "jobs": [{"id": j.id, "company": j.company, "title": j.title, "total": j.score.total} for j in jobs[:50]],
        "run": status(user_id),
    }


def status(user_id: str) -> dict:
    with _lock:
        return dict(_state.get(user_id, {"running": False}))


def cancel(user_id: str) -> bool:
    with _lock:
        if not _state.get(user_id, {}).get("running"):
            return False
        _cancel[user_id].set()
        _state[user_id]["cancelling"] = True
    return True


def research_job(user_id: str, job) -> int:
    """Research one job's company and save the result. Raises on a backend failure (nothing is saved)."""
    result = company_research.research_company(company=job.company, job_title=job.title, user_id=user_id)
    job.research = CompanyResearch(
        contacts=[Contact(**c) for c in result.get("contacts", [])],
        company_summary=result.get("company_summary", ""),
        researched_at=datetime.now(timezone.utc).isoformat(),
        searches_run=result.get("searches_run", []),
    )
    job.updated_at = datetime.now(timezone.utc).isoformat()
    storage.save_job(user_id, job)
    return len(job.research.contacts)


def research_missing(user_id: str, threshold: int, limit: int | None = None) -> dict:
    """Research the jobs from find_unresearched(). One run per user at a time; raises RuntimeError if one is running.

    Returns {"researched": [...], "errors": [...], "remaining": n, "cancelled": bool, "aborted": str}.
    """
    with _lock:
        if _state.get(user_id, {}).get("running"):
            raise RuntimeError("A company research run is already running")
        _state[user_id] = {"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                           "done": 0, "total": 0, "errors": []}
        stop = _cancel[user_id] = threading.Event()
    researched, errors, aborted, streak = [], [], "", 0
    try:
        jobs = find_unresearched(user_id, threshold)
        todo = jobs[:limit] if limit else jobs
        with _lock:
            _state[user_id]["total"] = len(todo)
        for job in todo:
            if stop.is_set():
                break
            try:
                job = storage.load_job(user_id, job.id)  # find_unresearched used shared cache objects
                n = research_job(user_id, job)
                researched.append({"id": job.id, "company": job.company, "contacts": n})
                streak = 0
            except Exception as e:
                logger.error(f"[{user_id}] Company research failed for {job.id} ({job.company}): {e}")
                errors.append(f"{job.id} ({job.company}): {e}")
                streak += 1
            with _lock:
                _state[user_id]["done"] += 1
                _state[user_id]["errors"] = list(errors)
            if streak >= MAX_CONSECUTIVE_FAILURES:
                aborted = f"stopped after {streak} failures in a row: {errors[-1]}"
                break
        return {"researched": researched, "errors": errors, "remaining": len(jobs) - len(todo),
                "cancelled": stop.is_set(), "aborted": aborted}
    finally:
        with _lock:
            _state[user_id].update(running=False, cancelling=False, cancelled=stop.is_set(), aborted=aborted,
                                   finished_at=datetime.now(timezone.utc).isoformat(),
                                   researched=len(researched))


def start_background(user_id: str, threshold: int, limit: int | None = None) -> None:
    """Run research_missing in a daemon thread (a full run outlasts an HTTP request)."""
    with _lock:
        if _state.get(user_id, {}).get("running"):
            raise RuntimeError("A company research run is already running")

    def _run():
        try:
            with ai_router.batch_mode():
                research_missing(user_id, threshold, limit)
        except Exception as e:
            logger.error(f"[{user_id}] Company research run crashed: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True, name=f"company-research-{user_id[:8]}").start()
