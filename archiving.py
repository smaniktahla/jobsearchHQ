"""
Archives stale jobs off the hot path (load_all_jobs) while keeping a lightweight
dedup fingerprint on disk (see storage.dedup_index) so scrapers never re-import
something we already looked at.

A job is archivable once it's both old AND either dead-ended or low value:
  - status is rejected or passed, or
  - it was scored below min_score

Anything still active (applied/interview/offer), unscored, or recently touched
is left alone regardless of how it would otherwise score, so nothing you're
actively tracking silently disappears from the tracker view.
"""

import logging
from datetime import datetime, timedelta, timezone

from models import Job, JobStatus
import storage

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {JobStatus.REJECTED, JobStatus.PASSED}
DEFAULT_MAX_AGE_DAYS = 60
DEFAULT_MIN_SCORE = 5


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_archivable(job: Job, cutoff: datetime, min_score: int) -> bool:
    updated = _parse_dt(job.updated_at)
    if updated is None or updated >= cutoff:
        return False
    if job.status in TERMINAL_STATUSES:
        return True
    if job.score is not None and job.score.total < min_score:
        return True
    return False


def run_archive_pass(
    user_id: str,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    min_score: int = DEFAULT_MIN_SCORE,
) -> dict:
    """Archive qualifying active jobs for one user. Returns a summary dict."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    jobs = storage.load_all_jobs(user_id)
    archived = []
    for job in jobs:
        if is_archivable(job, cutoff, min_score):
            if storage.archive_job(user_id, job.id):
                archived.append({"id": job.id, "title": job.title, "company": job.company})

    if archived:
        logger.info(f"[{user_id}] Archived {len(archived)} stale jobs (age>{max_age_days}d, score<{min_score})")

    return {
        "archived_count": len(archived),
        "archived": archived,
        "max_age_days": max_age_days,
        "min_score": min_score,
    }
