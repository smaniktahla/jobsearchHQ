"""Backfill tailored resumes for scored jobs that don't have one.

The daily pipeline only generates documents for jobs created in that run, so jobs scored later
(manual add, email/LinkedIn intake, re-score) never got a resume. This finds them and generates
the missing ones. Resumes only: cover letters stay a per-job action.
"""
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import docx_builder
import scoring
import storage
from models import JobStatus, MarketLane

logger = logging.getLogger(__name__)

# Jobs in these states are past the point where a new resume helps.
_CLOSED = {JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED, JobStatus.PASSED}

_lock = threading.Lock()
_state: dict[str, dict] = {}  # user_id -> progress of the latest/current run


def find_missing(user_id: str, threshold: int) -> list:
    """Active, scored jobs at/above threshold with no tailored resume file, best score first."""
    found = []
    for job in storage.load_all_jobs(user_id, copy=False):
        if not job.score or job.score.total < threshold:
            continue
        if job.status in _CLOSED or job.market_lane == MarketLane.IGNORE:
            continue
        if not job.raw_jd.strip():
            continue
        if job.tailored_resume_docx and Path(job.tailored_resume_docx).exists():
            continue
        found.append(job)
    found.sort(key=lambda j: (-j.score.total, j.created_at))
    return found


def summarize(user_id: str, threshold: int) -> dict:
    jobs = find_missing(user_id, threshold)
    return {
        "threshold": threshold,
        "count": len(jobs),
        "jobs": [{"id": j.id, "company": j.company, "title": j.title, "total": j.score.total} for j in jobs[:50]],
        "run": status(user_id),
    }


def status(user_id: str) -> dict:
    with _lock:
        return dict(_state.get(user_id, {"running": False}))


def generate_missing(user_id: str, threshold: int, limit: int | None = None) -> dict:
    """Generate resumes for jobs from find_missing(). One run per user at a time.

    Returns {"generated": [...], "errors": [...], "remaining": n}; raises RuntimeError if a run
    is already in progress.
    """
    with _lock:
        if _state.get(user_id, {}).get("running"):
            raise RuntimeError("A resume backfill is already running")
        _state[user_id] = {"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                           "done": 0, "total": 0, "errors": []}
    generated, errors = [], []
    try:
        jobs = find_missing(user_id, threshold)
        todo = jobs[:limit] if limit else jobs
        with _lock:
            _state[user_id]["total"] = len(todo)
        for job in todo:
            try:
                # Reload: find_missing used shared cache objects, which are read-only.
                job = storage.load_job(user_id, job.id)
                job.tailored_resume = scoring.generate_tailored_resume(job, user_id)
                job.tailored_resume_docx = docx_builder.generate_resume_docx(
                    job.tailored_resume, job.id, user_id, job.company, job.title
                )
                storage.save_job(user_id, job)
                generated.append({"id": job.id, "company": job.company, "title": job.title,
                                  "total": job.score.total})
            except Exception as e:
                logger.error(f"[{user_id}] Backfill resume failed for {job.id}: {e}")
                errors.append(f"{job.id} ({job.company}): {e}")
            with _lock:
                _state[user_id]["done"] += 1
                _state[user_id]["errors"] = list(errors)
        return {"generated": generated, "errors": errors, "remaining": len(jobs) - len(todo)}
    finally:
        with _lock:
            _state[user_id].update(running=False, finished_at=datetime.now(timezone.utc).isoformat(),
                                   generated=len(generated))


def start_background(user_id: str, threshold: int, limit: int | None = None) -> None:
    """Run generate_missing in a daemon thread (a full backfill outlasts an HTTP request)."""
    with _lock:
        if _state.get(user_id, {}).get("running"):
            raise RuntimeError("A resume backfill is already running")

    def _run():
        try:
            generate_missing(user_id, threshold, limit)
        except Exception as e:
            logger.error(f"[{user_id}] Resume backfill crashed: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True, name=f"resume-backfill-{user_id[:8]}").start()
