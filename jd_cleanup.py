"""
JD cleanup — finds jobs whose stored raw_jd looks like a broken scrape
(login walls, dead-posting placeholders, thin metadata stubs) and re-scrapes
them via intake.process_intake(). Useful after scraper fixes (e.g. a new
site-specific extractor) land, since previously-broken captures never
self-heal otherwise.
"""

import logging

import intake
import storage
from models import Job

logger = logging.getLogger(__name__)

JD_MARKERS = (
    "responsibilit", "requirement", "experience", "qualif", "skills",
    "role", "you will", "we are looking", "about the job", "about the role",
)
LOGIN_MARKERS = (
    "sign in", "join now", "forgot password", "agree & join",
    "sign in with email", "unable to load the page",
)

MIN_GOOD_LENGTH = 300
MAX_JUNK_LENGTH = 3000  # longer pages that hit a marker are probably a real JD with boilerplate mixed in


def is_junk_jd(raw_jd: str) -> bool:
    """Heuristic: does this look like a broken scrape rather than a real JD?"""
    if not raw_jd or not raw_jd.strip():
        return False
    if len(raw_jd) >= MAX_JUNK_LENGTH:
        return False
    lowered = raw_jd.lower()
    has_jd = any(m in lowered for m in JD_MARKERS)
    has_login = any(m in lowered for m in LOGIN_MARKERS)
    return has_login or not has_jd


def find_junk_jobs(user_id: str) -> list[Job]:
    jobs = storage.load_all_jobs(user_id)
    return [j for j in jobs if j.url and is_junk_jd(j.raw_jd)]


def rescrape_job(job: Job) -> bool:
    """
    Attempt to re-scrape a job's URL. Mutates job.raw_jd/title/pay_range in
    place and returns True if a real JD was recovered, False otherwise
    (caller should not save the job in that case).
    """
    scraped = intake.process_intake("url_scrape", job.url)
    new_raw = scraped.get("raw_jd", "")
    if len(new_raw) < MIN_GOOD_LENGTH or is_junk_jd(new_raw):
        return False
    job.raw_jd = new_raw
    if not job.title and scraped.get("title"):
        job.title = scraped["title"]
    if not job.pay_range and scraped.get("pay_range"):
        job.pay_range = scraped["pay_range"]
    return True


def cleanup_all(user_id: str) -> dict:
    """Non-interactive sweep for the scheduler — no live progress tracking."""
    candidates = find_junk_jobs(user_id)
    result = {"total": len(candidates), "fixed": 0, "unresolved": 0, "errors": []}
    for job in candidates:
        try:
            if rescrape_job(job):
                storage.save_job(user_id, job)
                result["fixed"] += 1
                logger.info(f"[jd_cleanup] fixed {job.id} ({job.company})")
            else:
                result["unresolved"] += 1
        except Exception as e:
            result["unresolved"] += 1
            result["errors"].append(f"{job.company}: {e}")
            logger.warning(f"[jd_cleanup] failed {job.id} ({job.company}): {e}")
    return result
