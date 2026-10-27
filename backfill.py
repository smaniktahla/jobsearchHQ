"""Backfill tailored resumes and cover letters for scored jobs that are missing them.

The daily pipeline only generates documents for jobs created in that run, so jobs scored later
(manual add, email/LinkedIn intake, re-score) never got them. This finds those jobs and fills in
whichever of the two is missing; a job is not ready to apply without both.
"""
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import ai_router
import docx_builder
import scoring
import auto_approve
import storage
from models import JobStatus, MarketLane

logger = logging.getLogger(__name__)

# Jobs in these states are past the point where a new resume helps.
_CLOSED = {JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED, JobStatus.PASSED}

_lock = threading.Lock()
_state: dict[str, dict] = {}  # user_id -> progress of the latest/current run
_cancel: dict[str, threading.Event] = {}  # user_id -> set to stop the current run


def _has_resume(job) -> bool:
    return bool(job.tailored_resume_docx and Path(job.tailored_resume_docx).exists())


def _has_letters(job) -> bool:
    return bool(job.cover_letters)


def find_missing(user_id: str, threshold: int) -> list:
    """Active, scored jobs at/above threshold missing a tailored resume file or cover letters, best score first."""
    found = []
    for job in storage.load_all_jobs(user_id, copy=False):
        if not job.score or job.score.total < threshold:
            continue
        if job.status in _CLOSED or job.market_lane == MarketLane.IGNORE:
            continue
        if not job.raw_jd.strip():
            continue
        if _has_resume(job) and _has_letters(job):
            continue
        found.append(job)
    found.sort(key=lambda j: (-j.score.total, j.created_at))
    return found


def summarize(user_id: str, threshold: int) -> dict:
    jobs = find_missing(user_id, threshold)
    return {
        "threshold": threshold,
        "count": len(jobs),
        "missing_resumes": sum(1 for j in jobs if not _has_resume(j)),
        "missing_letters": sum(1 for j in jobs if not _has_letters(j)),
        "jobs": [{"id": j.id, "company": j.company, "title": j.title, "total": j.score.total} for j in jobs[:50]],
        "run": status(user_id),
    }


def status(user_id: str) -> dict:
    with _lock:
        return dict(_state.get(user_id, {"running": False}))


def cancel(user_id: str) -> bool:
    """Ask the current run to stop. The resume being generated finishes; no further ones start."""
    with _lock:
        if not _state.get(user_id, {}).get("running"):
            return False
        _cancel[user_id].set()
        _state[user_id]["cancelling"] = True
    return True


def generate_missing(user_id: str, threshold: int, limit: int | None = None) -> dict:
    """Generate the missing resume and/or cover letters for jobs from find_missing(). One run per user at a time.

    Returns {"generated": [...], "errors": [...], "remaining": n}; raises RuntimeError if a run
    is already in progress.
    """
    with _lock:
        if _state.get(user_id, {}).get("running"):
            raise RuntimeError("A resume backfill is already running")
        _state[user_id] = {"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                           "done": 0, "total": 0, "errors": []}
        stop = _cancel[user_id] = threading.Event()
    generated, errors, approved = [], [], []
    config = storage.load_config(user_id)
    try:
        jobs = find_missing(user_id, threshold)
        todo = jobs[:limit] if limit else jobs
        with _lock:
            _state[user_id]["total"] = len(todo)
        for job in todo:
            if stop.is_set():
                break
            try:
                # Reload: find_missing used shared cache objects, which are read-only.
                job = storage.load_job(user_id, job.id)
                if not _has_resume(job):
                    job.tailored_resume = scoring.generate_tailored_resume(job, user_id)
                    job.tailored_resume_docx = docx_builder.generate_resume_docx(
                        job.tailored_resume, job.id, user_id, job.company, job.title
                    )
                    storage.save_job(user_id, job)  # keep the resume even if the letters fail
                if not _has_letters(job):
                    letters = scoring.generate_cover_letters(job, user_id)
                    for letter in letters:
                        letter.docx_path = docx_builder.generate_cover_letter_docx(
                            content=letter.content, variant=letter.variant, job_id=job.id,
                            user_id=user_id, company=job.company, title=job.title,
                            author_name=config.author_name, author_location=config.author_location,
                            author_phone=config.author_phone,
                            author_email=config.smtp_user or config.follow_up_email,
                        )
                    job.cover_letters = letters
                    storage.save_job(user_id, job)
                generated.append({"id": job.id, "company": job.company, "title": job.title,
                                  "total": job.score.total})
                # Approve as documents land (not only at the end of a long run); one email at the end.
                approved += auto_approve.run(user_id, config, notify=False)["approved"]
            except Exception as e:
                logger.error(f"[{user_id}] Backfill resume failed for {job.id}: {e}")
                errors.append(f"{job.id} ({job.company}): {e}")
            with _lock:
                _state[user_id]["done"] += 1
                _state[user_id]["errors"] = list(errors)
        return {"generated": generated, "errors": errors, "remaining": len(jobs) - len(todo),
                "cancelled": stop.is_set()}
    finally:
        if approved:
            auto_approve.send_summary(config, approved, 0)
        with _lock:
            _state[user_id].update(running=False, cancelling=False, cancelled=stop.is_set(),
                                   finished_at=datetime.now(timezone.utc).isoformat(),
                                   generated=len(generated))


def start_background(user_id: str, threshold: int, limit: int | None = None) -> None:
    """Run generate_missing in a daemon thread (a full backfill outlasts an HTTP request)."""
    with _lock:
        if _state.get(user_id, {}).get("running"):
            raise RuntimeError("A resume backfill is already running")

    def _run():
        try:
            with ai_router.batch_mode():
                generate_missing(user_id, threshold, limit)
        except Exception as e:
            logger.error(f"[{user_id}] Resume backfill crashed: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True, name=f"resume-backfill-{user_id[:8]}").start()
