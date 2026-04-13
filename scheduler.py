"""
Scheduler for Job Search Command Center.
Runs automated daily job board searches, scoring, and resume/cover letter generation.
Uses APScheduler BackgroundScheduler so it coexists with FastAPI's sync workers.
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

# Module-level scheduler instance
_scheduler: BackgroundScheduler | None = None

LOG_DIR = Path("/app/data/scheduler_logs")


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="America/New_York")
    return _scheduler


def start_scheduler():
    """Initialize and start the scheduler based on saved config."""
    sched = get_scheduler()
    if sched.running:
        return
    sched.start()
    logger.info("Scheduler started")
    config = storage.load_config()
    if config.scheduled_search_enabled:
        _apply_schedule(config)


def shutdown_scheduler():
    """Gracefully shut down the scheduler."""
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("Scheduler shut down")


def update_schedule(config: AppConfig):
    """Update the scheduled search job based on config changes."""
    _apply_schedule(config)


def _apply_schedule(config: AppConfig):
    """Internal: add/replace the daily search cron job."""
    sched = get_scheduler()
    if not sched.running:
        return

    # Remove existing job if present
    if sched.get_job("daily_search"):
        sched.remove_job("daily_search")
        logger.info("Removed existing daily_search job")

    if not config.scheduled_search_enabled:
        logger.info("Scheduled search is disabled")
        return

    # Parse schedule_time "HH:MM"
    try:
        parts = config.scheduled_search_time.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        hour, minute = 7, 0
        logger.warning(f"Invalid schedule_time '{config.scheduled_search_time}', defaulting to 07:00")

    trigger = CronTrigger(hour=hour, minute=minute)
    sched.add_job(
        run_daily_pipeline,
        trigger=trigger,
        id="daily_search",
        name="Daily Job Board Search",
        replace_existing=True,
    )
    logger.info(f"Scheduled daily search at {hour:02d}:{minute:02d} ET")


def get_schedule_status() -> dict:
    """Return current scheduler status for the API/UI."""
    sched = get_scheduler()
    config = storage.load_config()
    job = sched.get_job("daily_search") if sched.running else None

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


def run_daily_pipeline() -> dict:
    """
    The main automated pipeline:
    1. Run job board searches with configured parameters
    2. Optionally auto-score new jobs that have descriptions
    3. Optionally auto-generate resumes/cover letters for high scorers
    """
    config = storage.load_config()
    run_log = {
        "started_at": datetime.now().isoformat(),
        "search_results": [],
        "scored": [],
        "generated": [],
        "errors": [],
    }

    logger.info("=== Daily search pipeline started ===")

    # --- Step 1: Search job boards ---
    search_terms = [t.strip() for t in config.scheduled_search_terms if t.strip()]
    if not search_terms:
        logger.warning("No search terms configured, skipping search")
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
            result = jobspy_search.run_search(req)
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
                f"Search '{term}': {result.total_scraped} scraped, "
                f"{len(result.created)} new, {len(result.skipped)} dupes"
            )
        except Exception as e:
            logger.error(f"Search failed for '{term}': {e}")
            run_log["errors"].append(f"Search '{term}': {str(e)}")

    # --- Step 2: Auto-score new jobs ---
    if config.auto_score_new_jobs and all_created_ids:
        logger.info(f"Auto-scoring {len(all_created_ids)} new jobs...")
        for job_id in all_created_ids:
            try:
                job = storage.load_job(job_id)
                if not job or not job.raw_jd or len(job.raw_jd.strip()) < 200:
                    continue  # Skip jobs without real descriptions

                score_result = scoring.score_job(job)
                job.score = score_result
                job.update_status(JobStatus.SCORED)
                if score_result.recommended_lane:
                    job.market_lane = score_result.recommended_lane
                storage.save_job(job)

                run_log["scored"].append({
                    "id": job_id,
                    "title": job.title,
                    "company": job.company,
                    "total": score_result.total,
                })
                logger.info(f"Scored: {job.company} - {job.title} → {score_result.total}/10")
            except Exception as e:
                logger.error(f"Scoring failed for {job_id}: {e}")
                run_log["errors"].append(f"Score {job_id}: {str(e)}")

    # --- Step 3: Auto-generate resume/cover letters for high scorers ---
    if config.auto_generate_above_threshold and all_created_ids:
        threshold = config.auto_generate_threshold
        logger.info(f"Auto-generating for jobs scoring >= {threshold}...")

        for job_id in all_created_ids:
            try:
                job = storage.load_job(job_id)
                if not job or not job.score:
                    continue

                total = job.score.total if job.score else 0
                if total < threshold:
                    continue

                # Generate tailored resume
                try:
                    resume_text = scoring.generate_tailored_resume(job)
                    job.tailored_resume = resume_text
                    docx_path = docx_builder.generate_resume_docx(
                        resume_text, job.id, job.company, job.title
                    )
                    job.tailored_resume_docx = docx_path
                    storage.save_job(job)
                    logger.info(f"Generated resume for: {job.company} - {job.title}")
                except Exception as e:
                    logger.error(f"Resume generation failed for {job_id}: {e}")
                    run_log["errors"].append(f"Resume {job_id}: {str(e)}")

                # Generate cover letters
                try:
                    letters = scoring.generate_cover_letters(job)
                    job.cover_letters = letters
                    storage.save_job(job)
                    logger.info(f"Generated cover letters for: {job.company} - {job.title}")
                except Exception as e:
                    logger.error(f"Cover letter generation failed for {job_id}: {e}")
                    run_log["errors"].append(f"Cover letter {job_id}: {str(e)}")

                run_log["generated"].append({
                    "id": job_id,
                    "title": job.title,
                    "company": job.company,
                    "total": total,
                    "variant": job.score.recommended_resume if job.score else "base",
                })

            except Exception as e:
                logger.error(f"Generation pipeline failed for {job_id}: {e}")
                run_log["errors"].append(f"Generate {job_id}: {str(e)}")

    run_log["finished_at"] = datetime.now().isoformat()
    _save_run_log(run_log)

    summary = (
        f"Pipeline complete: {sum(r['created'] for r in run_log['search_results'])} new jobs, "
        f"{len(run_log['scored'])} scored, {len(run_log['generated'])} generated, "
        f"{len(run_log['errors'])} errors"
    )
    logger.info(f"=== {summary} ===")
    return run_log


def run_pipeline_now() -> dict:
    """Manually trigger the daily pipeline (called from API endpoint)."""
    return run_daily_pipeline()


def get_recent_logs(count: int = 10) -> list[dict]:
    """Return the most recent scheduler run logs."""
    if not LOG_DIR.exists():
        return []
    logs = []
    for f in sorted(LOG_DIR.glob("run_*.json"), reverse=True)[:count]:
        try:
            logs.append(json.loads(f.read_text()))
        except Exception:
            continue
    return logs


def _save_run_log(run_log: dict):
    """Persist the run log for review in the UI."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"run_{timestamp}.json"
    log_path.write_text(json.dumps(run_log, indent=2))

    # Keep only last 30 logs
    logs = sorted(LOG_DIR.glob("run_*.json"), reverse=True)
    for old_log in logs[30:]:
        old_log.unlink()
