"""Auto-approve scored jobs once their documents are ready, so JobPilot can pick them up.

ready_to_apply needs review_status == APPROVED. This sets it for jobs that clear the user's
threshold AND already have a tailored resume file and cover letters, so an application is never
greenlit without its documents. Only PENDING jobs are touched: a manual reject/approve/pass is
never overridden. Off by default (AppConfig.auto_approve_enabled), capped per rolling 24 hours.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage
from models import AgentEvent, AgentEventType, AppConfig, JobStatus, MarketLane, ReviewStatus

logger = logging.getLogger(__name__)

_CLOSED = {JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED, JobStatus.PASSED}
NOTE_PREFIX = "auto-approved"


def is_eligible(job, threshold: int) -> bool:
    return bool(
        job.score and job.score.total >= threshold
        and job.review_status == ReviewStatus.PENDING
        and job.market_lane != MarketLane.IGNORE
        and job.posting_status != "closed"
        and job.status not in _CLOSED
        and job.cover_letters
        and job.tailored_resume_docx and Path(job.tailored_resume_docx).exists()
    )


def _approved_last_24h(jobs) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    n = 0
    for j in jobs:
        for e in j.agent_log:
            if e.notes.startswith(NOTE_PREFIX):
                try:
                    if datetime.fromisoformat(e.at) >= cutoff:
                        n += 1
                except ValueError:
                    pass
    return n


def run(user_id: str, config: AppConfig | None = None) -> dict:
    """Approve eligible jobs (best score first) up to the daily cap.

    Returns {"approved": [...], "waiting": n, "enabled": bool}.
    """
    config = config or storage.load_config(user_id)
    if not config.auto_approve_enabled or config.job_search_paused:
        return {"approved": [], "waiting": 0, "enabled": False}
    threshold = config.auto_approve_threshold
    jobs = storage.load_all_jobs(user_id, copy=False)
    eligible = sorted((j for j in jobs if is_eligible(j, threshold)),
                      key=lambda j: (-j.score.total, j.created_at))
    room = max(0, config.auto_approve_daily_cap - _approved_last_24h(jobs))
    approved = []
    for stale in eligible[:room]:
        job = storage.load_job(user_id, stale.id)  # cache objects are read-only
        if not job or not is_eligible(job, threshold):
            continue
        job.review_status = ReviewStatus.APPROVED
        job.agent_log.append(AgentEvent(
            event_type=AgentEventType.RESOLVE, success=True,
            notes=f"{NOTE_PREFIX} (score {job.score.total} ≥ {threshold})"))
        job.updated_at = datetime.now(timezone.utc).isoformat()
        storage.save_job(user_id, job)
        approved.append({"id": job.id, "company": job.company, "title": job.title, "total": job.score.total})
    waiting = len(eligible) - len(approved)
    if approved:
        _notify(config, approved, waiting, threshold)
    logger.info(f"[{user_id}] Auto-approved {len(approved)} jobs, {waiting} waiting")
    return {"approved": approved, "waiting": waiting, "enabled": True}


def _notify(config: AppConfig, approved: list[dict], waiting: int, threshold: int) -> None:
    """One summary email per run listing what JobPilot may now apply to."""
    if not (config.smtp_user and config.smtp_password and config.author_email):
        return
    try:
        import email_service
        lines = [f"Auto-approved {len(approved)} job(s) scoring >= {threshold}; JobPilot can now pick them up:", ""]
        lines += [f"  {a['total']}/10  {a['company']} - {a['title']}" for a in approved]
        if waiting:
            lines += ["", f"{waiting} more are eligible and wait for tomorrow (daily cap {config.auto_approve_daily_cap})."]
        lines += ["", "Reject any job in JSHQ to keep it out; turn auto-approve off in Settings > Auto-Processing."]
        email_service.send_email(config, config.author_email,
                                 f"JSHQ auto-approved {len(approved)} job(s)", "\n".join(lines))
    except Exception as e:
        logger.warning(f"Auto-approve summary email failed: {e}")
