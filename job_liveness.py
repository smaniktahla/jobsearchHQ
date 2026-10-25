"""
Posting liveness — checks whether a tracked job's posting is still open.

Sets Job.posting_status to "open", "closed" or "unknown" (never touches the
application workflow Job.status). Evidence is conservative: "closed" needs a
hard signal (404/410, a known closed-posting phrase, or an expired JSON-LD
validThrough); "open" needs positive evidence (a live JobPosting or a real JD
with no closed signals). Anything blocked, login-walled or ambiguous stays
"unknown" and is retried later, so a flaky site never closes a live job.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

import company_site_search
import storage
from models import Job, JobStatus

logger = logging.getLogger(__name__)

# Application-stage jobs are tracked by hand; a closed posting there is noise.
SKIP_STATUSES = {JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED, JobStatus.PASSED}

RECHECK_OPEN_DAYS = 7
RECHECK_UNKNOWN_DAYS = 14
HOST_MIN_INTERVAL = 2.0   # seconds between requests to the same host
SLOW_HOSTS = {"www.linkedin.com": 4.0, "www.indeed.com": 4.0}   # be gentler where we're likelier to be rate-limited
DEFAULT_BATCH = 300

CLOSED_PHRASES = (
    "no longer accepting applications",
    "no longer available",
    "this job has expired",
    "job has expired",
    "this job has been closed",
    "job posting has been closed",
    "posting is closed",
    "position has been filled",
    "position is no longer open",
    "this position is no longer",
    "job is no longer open",
    "no longer open for applications",
    "this requisition is no longer",
    "we couldn't find that job",
    "the job you are looking for",
    "this opportunity is no longer",
    "role has been filled",
    "applications are closed",
)
LOGIN_WALL = ("sign in to view", "join now", "authwall", "log in to continue")

_JSONLD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)

_host_last: dict[str, float] = {}
_host_lock = threading.Lock()


def _throttle(host: str) -> None:
    with _host_lock:
        now = time.monotonic()
        wait = _host_last.get(host, 0) + SLOW_HOSTS.get(host, HOST_MIN_INTERVAL) - now
        _host_last[host] = now + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


def _jobposting_nodes(html: str):
    for m in _JSONLD.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if "@graph" in n:
                    stack.extend(n["@graph"])
                t = n.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    yield n
            elif isinstance(n, list):
                stack.extend(n)


def _expired(valid_through: str) -> bool:
    try:
        dt = datetime.fromisoformat(valid_through.replace("Z", "+00:00"))
    except Exception:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc)


def classify(url: str, status_code: int, final_url: str, html: str) -> tuple[str, str]:
    """Return (posting_status, reason). Pure function — unit-testable without network."""
    if status_code in (404, 410):
        return "closed", f"HTTP {status_code}"
    if status_code >= 400:
        return "unknown", f"HTTP {status_code}"
    lowered = html.lower()
    # Greenhouse sends dead postings to the board root with ?error=true
    if "error=true" in final_url and "greenhouse" in final_url:
        return "closed", "Greenhouse redirect (posting removed)"
    for node in _jobposting_nodes(html):
        vt = str(node.get("validThrough") or "")
        if vt and _expired(vt):
            return "closed", f"validThrough {vt[:10]} passed"
    for phrase in CLOSED_PHRASES:
        if phrase in lowered:
            # "the job you are looking for" is only a closed signal on short pages
            if phrase == "the job you are looking for" and len(html) > 20000:
                continue
            return "closed", f'page says "{phrase}"'
    if any(w in lowered for w in LOGIN_WALL) and len(html) < 15000:
        return "unknown", "login wall"
    if any(True for _ in _jobposting_nodes(html)):
        return "open", "live JobPosting markup"
    if company_site_search._has_real_job_description(re.sub(r"<[^>]+>", " ", html)):
        return "open", "job description present"
    return "unknown", "no clear signal"


def check_url(url: str) -> tuple[str, str]:
    host = urlparse(url).netloc.lower()
    _throttle(host)
    try:
        with httpx.Client(follow_redirects=True, timeout=20, headers=company_site_search._headers()) as c:
            r = c.get(url)
        return classify(url, r.status_code, str(r.url), r.text)
    except httpx.HTTPError as e:
        return "unknown", f"fetch failed: {type(e).__name__}"


def check_job(job: Job) -> bool:
    """Check one job in place; returns False if it has no URL. Caller saves."""
    if not job.url.strip():
        return False
    status, reason = check_url(job.url)
    now = datetime.now(timezone.utc).isoformat()
    if status == "closed" and job.posting_status != "closed":
        job.posting_closed_at = now
    job.posting_status, job.posting_reason, job.posting_checked_at = status, reason, now
    return True


def _due(job: Job, now: datetime, min_score: int = 0) -> bool:
    if not job.url.strip() or job.status in SKIP_STATUSES or job.posting_status == "closed":
        return False
    if job.score is None or job.score.total < min_score:
        return False
    if not job.posting_checked_at:
        return True
    try:
        last = datetime.fromisoformat(job.posting_checked_at)
    except ValueError:
        return True
    days = RECHECK_OPEN_DAYS if job.posting_status == "open" else RECHECK_UNKNOWN_DAYS
    return now - last >= timedelta(days=days)


def due_jobs(user_id: str, limit: int) -> list[Job]:
    now = datetime.now(timezone.utc)
    min_score = storage.load_config(user_id).tracking_min_score
    jobs = [j for j in storage.load_all_jobs(user_id, copy=False) if _due(j, now, min_score)]
    # never-checked first, then highest score, so the best leads are verified soonest
    jobs.sort(key=lambda j: (bool(j.posting_checked_at), -(j.score.total if j.score else -1)))
    return jobs[:limit]


# ── Run state (one run per user at a time) ───────────────────────────────────

_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()


def run_status(user_id: str) -> dict:
    with _runs_lock:
        return dict(_runs.get(user_id, {"running": False}))


def run_pass(user_id: str, limit: int = DEFAULT_BATCH) -> dict:
    with _runs_lock:
        if _runs.get(user_id, {}).get("running"):
            return dict(_runs[user_id])
        state = {"running": True, "total": 0, "done": 0, "open": 0, "closed": 0, "unknown": 0,
                 "started_at": datetime.now(timezone.utc).isoformat()}
        _runs[user_id] = state
    try:
        batch = due_jobs(user_id, limit)
        state["total"] = len(batch)
        for stale in batch:
            # re-load a private copy so we never mutate the shared cache objects
            job = storage.load_job(user_id, stale.id)
            if job is None:
                state["done"] += 1
                continue
            try:
                check_job(job)
                storage.save_job(user_id, job)
                state[job.posting_status] += 1
            except Exception as e:
                logger.warning(f"[{user_id}] posting check failed for {job.id}: {e}")
            state["done"] += 1
        logger.info(f"[{user_id}] Posting check: {state['done']} checked, {state['closed']} closed, "
                    f"{state['open']} open, {state['unknown']} unknown")
    finally:
        state["running"] = False
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
    return dict(state)
