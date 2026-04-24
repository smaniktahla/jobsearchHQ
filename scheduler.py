"""
Scheduler for Job Search HQ v2.
Runs automated daily job board searches, scoring, and resume/cover letter generation.

v2 architecture: per-user storage under /app/data/users/{user_id}/
- start_scheduler()        — called at app startup, no user context needed
- update_schedule()        — called when a user saves config; re-applies their cron job
- run_pipeline_now()       — manual trigger for a specific user
- get_schedule_status()    — status for a specific user
- shutdown_scheduler()     — called at app shutdown
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import storage
import scoring
import jobspy_search
import docx_builder
from models import AppConfig, JobStatus

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

LOG_DIR = Path("/app/data/scheduler_logs")


# ── Scheduler lifecycle ────────────────────────────────────────────────────────

def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="America/New_York")
    return _scheduler


def start_scheduler():
    """
    Called once at app startup — no user context available here.
    Discovers all existing users and schedules any that have scheduled_search_enabled=True.
    """
    sched = get_scheduler()
    if sched.running:
        return
    sched.start()
    logger.info("Scheduler started")

    # Walk all user dirs and re-apply their schedules
    users_dir = Path("/app/data/users")
    if not users_dir.exists():
        return
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        try:
            config = storage.load_config(user_id)
            if config.scheduled_search_enabled:
                _apply_schedule(user_id, config)
                logger.info(f"Restored schedule for user {user_id}")
        except Exception as e:
            logger.warning(f"Could not restore schedule for user {user_id}: {e}")


def shutdown_scheduler():
    """Gracefully shut down the scheduler."""
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("Scheduler shut down")


# ── Per-user schedule management ──────────────────────────────────────────────

def update_schedule(config: AppConfig, user_id: str):
    """Called when a user saves their config. Re-applies or removes their cron job."""
    _apply_schedule(user_id, config)


def _apply_schedule(user_id: str, config: AppConfig):
    """Add/replace/remove the cron job for a specific user."""
    sched = get_scheduler()
    if not sched.running:
        return

    job_id = f"daily_search_{user_id}"

    if sched.get_job(job_id):
        sched.remove_job(job_id)
        logger.info(f"Removed existing schedule for user {user_id}")

    if not config.scheduled_search_enabled:
        logger.info(f"Scheduled search disabled for user {user_id}")
        return

    try:
        parts = config.scheduled_search_time.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        hour, minute = 7, 0
        logger.warning(
            f"Invalid schedule_time '{config.scheduled_search_time}' for user {user_id}, "
            f"defaulting to 07:00"
        )

    sched.add_job(
        run_daily_pipeline,
        trigger=CronTrigger(hour=hour, minute=minute),
        id=job_id,
        name=f"Daily Search — {user_id}",
        replace_existing=True,
        kwargs={"user_id": user_id},
    )
    logger.info(f"Scheduled daily search for user {user_id} at {hour:02d}:{minute:02d} ET")


# ── Status & logs ──────────────────────────────────────────────────────────────

def get_schedule_status(user_id: str) -> dict:
    """Return current scheduler status for a specific user."""
    sched = get_scheduler()
    config = storage.load_config(user_id)
    job_id = f"daily_search_{user_id}"
    job = sched.get_job(job_id) if sched.running else None

    return {
        "scheduler_running": sched.running if sched else False,
        "enabled": config.scheduled_search_enabled,
        "schedule_time": config.scheduled_search_time,
        "next_run": str(job.next_run_time) if job else None,
        "auto_score": config.auto_score_new_jobs,
        "auto_generate": config.auto_generate_above_threshold,
        "score_threshold": config.auto_generate_threshold,
        "search_terms": config.scheduled_search_terms,
        "search_sites": config.scheduled_search_sites,
        "search_location": config.scheduled_search_location,
        "search_results_wanted": config.scheduled_search_results,
        "search_hours_old": config.scheduled_search_hours_old,
    }


def get_recent_logs(count: int = 10) -> list[dict]:
    """Return the most recent scheduler run logs (across all users)."""
    if not LOG_DIR.exists():
        return []
    logs = []
    for f in sorted(LOG_DIR.glob("run_*.json"), reverse=True)[:count]:
        try:
            logs.append(json.loads(f.read_text()))
        except Exception:
            continue
    return logs


# ── Pipeline ───────────────────────────────────────────────────────────────────

def run_pipeline_now(user_id: str) -> dict:
    """Manually trigger the pipeline for a specific user."""
    return run_daily_pipeline(user_id=user_id)


def run_daily_pipeline(user_id: str) -> dict:
    """
    The main automated pipeline for a single user:
    1. Run job board searches with the user's configured parameters
    2. Optionally auto-score new jobs that have descriptions
    3. Optionally auto-generate resumes/cover letters for high scorers
    """
    config = storage.load_config(user_id)
    run_log = {
        "user_id": user_id,
        "started_at": datetime.now().isoformat(),
        "search_results": [],
        "scored": [],
        "generated": [],
        "errors": [],
    }

    logger.info(f"=== Daily pipeline started for user {user_id} ===")

    # ── Step 1: Search job boards ──────────────────────────────────────────────
    search_terms = [t.strip() for t in config.scheduled_search_terms if t.strip()]
    if not search_terms:
        logger.warning(f"No search terms configured for user {user_id}, skipping search")
        run_log["errors"].append("No search terms configured")
        _save_run_log(run_log)
        return run_log

    all_created_ids = []

    for term in search_terms:
        try:
            req = jobspy_search.JobSearchRequest(
                search_term=term,
                location=config.scheduled_search_location,
                sites=config.scheduled_search_sites,
                results_wanted=config.scheduled_search_results,
                hours_old=config.scheduled_search_hours_old,
                is_remote=config.scheduled_search_remote,
                linkedin_fetch_description=True,
                skip_existing=True,
            )
            result = jobspy_search.run_search(req, user_id)
            created_ids = [j["id"] for j in result.created]
            all_created_ids.extend(created_ids)

            run_log["search_results"].append({
                "term": term,
                "created": len(result.created),
                "skipped": len(result.skipped),
                "errors": len(result.errors),
                "total_scraped": result.total_scraped,
            })
            logger.info(
                f"[{user_id}] Search '{term}': {result.total_scraped} scraped, "
                f"{len(result.created)} new, {len(result.skipped)} dupes"
            )
        except Exception as e:
            logger.error(f"[{user_id}] Search failed for '{term}': {e}")
            run_log["errors"].append(f"Search '{term}': {str(e)}")

    # ── Step 2: Auto-score new jobs ────────────────────────────────────────────
    if config.auto_score_new_jobs and all_created_ids:
        logger.info(f"[{user_id}] Auto-scoring {len(all_created_ids)} new jobs...")
        for job_id in all_created_ids:
            job = None
            try:
                job = storage.load_job(user_id, job_id)
                if not job or not job.raw_jd or len(job.raw_jd.strip()) < 200:
                    continue

                score_result = scoring.score_job(job, user_id)
                job.score = score_result
                job.update_status(JobStatus.SCORED)
                if score_result.recommended_lane:
                    job.market_lane = score_result.recommended_lane
                storage.save_job(user_id, job)

                run_log["scored"].append({
                    "id": job_id,
                    "title": job.title,
                    "company": job.company,
                    "total": score_result.total,
                })
                logger.info(
                    f"[{user_id}] Scored: {job.company} - {job.title} → {score_result.total}/10"
                )
            except Exception as e:
                logger.error(f"[{user_id}] Scoring failed for {job_id}: {e}", exc_info=True)
                job_label = f"{job.company} - {job.title}" if job else job_id
                run_log["errors"].append(f"Score {job_id} ({job_label}): {str(e)}")

    # ── Step 3: Auto-generate resume/cover letters for high scorers ────────────
    if config.auto_generate_above_threshold and all_created_ids:
        threshold = config.auto_generate_threshold
        logger.info(f"[{user_id}] Auto-generating for jobs scoring >= {threshold}...")

        for job_id in all_created_ids:
            job = None
            try:
                job = storage.load_job(user_id, job_id)
                if not job or not job.score:
                    continue
                if job.score.total < threshold:
                    continue

                # Generate tailored resume
                try:
                    resume_text = scoring.generate_tailored_resume(job, user_id)
                    job.tailored_resume = resume_text
                    docx_path = docx_builder.generate_resume_docx(
                        resume_text, job.id, user_id, job.company, job.title
                    )
                    job.tailored_resume_docx = docx_path
                    storage.save_job(user_id, job)
                    logger.info(
                        f"[{user_id}] Generated resume for: {job.company} - {job.title}"
                    )
                except Exception as e:
                    logger.error(f"[{user_id}] Resume generation failed for {job_id}: {e}")
                    run_log["errors"].append(f"Resume {job_id}: {str(e)}")

                # Generate cover letters
                try:
                    letters = scoring.generate_cover_letters(job, user_id)
                    job.cover_letters = letters
                    storage.save_job(user_id, job)
                    logger.info(
                        f"[{user_id}] Generated cover letters for: {job.company} - {job.title}"
                    )
                except Exception as e:
                    logger.error(f"[{user_id}] Cover letter gen failed for {job_id}: {e}")
                    run_log["errors"].append(f"Cover letter {job_id}: {str(e)}")

                run_log["generated"].append({
                    "id": job_id,
                    "title": job.title,
                    "company": job.company,
                    "total": job.score.total,
                    "variant": job.score.recommended_resume if job.score else "base",
                })

            except Exception as e:
                logger.error(
                    f"[{user_id}] Generation pipeline failed for {job_id}: {e}", exc_info=True
                )
                job_label = f"{job.company} - {job.title}" if job else job_id
                run_log["errors"].append(f"Generate {job_id} ({job_label}): {str(e)}")

    run_log["finished_at"] = datetime.now().isoformat()
    _save_run_log(run_log)

    total_new = sum(r["created"] for r in run_log["search_results"])
    logger.info(
        f"=== [{user_id}] Pipeline complete: {total_new} new, "
        f"{len(run_log['scored'])} scored, {len(run_log['generated'])} generated, "
        f"{len(run_log['errors'])} errors ==="
    )
    return run_log


def _save_run_log(run_log: dict):
    """Persist the run log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    user_id = run_log.get("user_id", "unknown")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"run_{user_id}_{timestamp}.json"
    log_path.write_text(json.dumps(run_log, indent=2))

    # Keep only last 30 logs total
    logs = sorted(LOG_DIR.glob("run_*.json"), reverse=True)
    for old_log in logs[30:]:
        old_log.unlink()
