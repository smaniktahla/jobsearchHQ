import asyncio
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import (
    get_current_user,
    get_admin_user_id,
    get_agent_api_key,
    verify_agent_api_key,
    invalidate_discovery_cache,
    is_admin,
    is_setup_complete,
    load_system_config,
    login_handler,
    callback_handler,
    logout_handler,
    save_system_config,
)
from pydantic import BaseModel
from models import (
    ReviewStatus, AgentEvent, AgentEventType, AppConfig, ApplicationResult,
    EmailCompose, EmailRecord, IntakeSource, Job, JobCreate,
    JobStatus, JobUpdate, MarketLane, ProfileUpdate, User,
)
import docx_builder
import email_service
import jobspy_search
import linkedin_intake
import scoring
import storage
import company_research
import company_site_search
import ai_router
import scheduler
from intake import process_intake

app = FastAPI(title="Job Search HQ", version="2.2")


# ── Lifecycle ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    storage.ensure_dirs()
    docx_builder.ensure_dirs()
    scheduler.start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown_scheduler()


# ── Helpers ───────────────────────────────────────────────────────────────────

def enrich_job(job: Job) -> dict:
    d = job.model_dump()
    d["follow_up_due"] = job.follow_up_due
    d["days_since_applied"] = job.days_since_applied
    d["ready_to_apply"] = job.ready_to_apply
    return d


# ── Auth & Setup routes (no auth required) ────────────────────────────────────

@app.get("/auth/login")
async def route_login(request: Request):
    return await login_handler(request)


@app.get("/auth/callback")
async def route_callback(request: Request):
    return await callback_handler(request)


@app.get("/auth/logout")
async def route_logout(request: Request):
    return await logout_handler(request)


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Returns current user info, 401 if not logged in, 503 if setup needed."""
    if not is_setup_complete():
        raise HTTPException(503, detail="setup_required")
    from auth import get_session_from_cookie
    session = get_session_from_cookie(request)
    if not session:
        raise HTTPException(401, detail="not_authenticated")
    user_id = session["user_id"]
    return {
        "id": user_id,
        "email": session["email"],
        "name": session["name"],
        "is_admin": is_admin(user_id),
    }


@app.get("/setup")
async def serve_setup():
    return FileResponse("static/setup.html")


@app.get("/api/setup")
async def get_setup_config():
    """Return current OIDC config (secrets masked) for the setup form."""
    cfg = load_system_config()
    return {
        "oidc_issuer": cfg.get("oidc_issuer", ""),
        "oidc_client_id": cfg.get("oidc_client_id", ""),
        "oidc_client_secret": "••••••••" if cfg.get("oidc_client_secret") else "",
        "oidc_redirect_uri": cfg.get("oidc_redirect_uri", ""),
        "is_complete": is_setup_complete(),
    }


@app.post("/api/setup")
async def save_setup_config(request: Request):
    """Save OIDC configuration. Accessible without auth (needed for first run)."""
    body = await request.json()
    cfg = load_system_config()
    cfg["oidc_issuer"] = body.get("oidc_issuer", "").rstrip("/")
    cfg["oidc_client_id"] = body.get("oidc_client_id", "")
    cfg["oidc_redirect_uri"] = body.get("oidc_redirect_uri", "")
    # Only update secret if a real value was provided (not the masked placeholder)
    secret = body.get("oidc_client_secret", "")
    if secret and not secret.startswith("•"):
        cfg["oidc_client_secret"] = secret
    save_system_config(cfg)
    invalidate_discovery_cache()
    return {"ok": True, "is_complete": is_setup_complete()}


# ── Agent endpoints (API key auth — for job-agent automation) ─────────────────

@app.get("/api/agent/ready-queue")
def agent_ready_queue(request: Request):
    """
    Returns ready-to-apply jobs for the admin user.
    Requires X-API-Key header. Key is auto-generated and stored in system_config.json.
    """
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    if not admin_id:
        raise HTTPException(503, "No admin user configured")
    jobs = storage.load_all_jobs(admin_id)
    queue = [enrich_job(j) for j in jobs if j.ready_to_apply]
    queue.sort(key=lambda j: (j.get("score") or {}).get("total", 0), reverse=True)
    return {"count": len(queue), "jobs": queue}


@app.get("/api/agent/profile")
def agent_get_profile(request: Request):
    """Agent-accessible profile endpoint — uses X-API-Key auth."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    if not admin_id:
        raise HTTPException(503, "No admin user configured")
    config = storage.load_config(admin_id)
    return {
        "name": config.author_name,
        "email": config.author_email or config.smtp_user or config.follow_up_email,
        "phone": config.author_phone,
        "location": config.author_location,
        "address": config.author_address,
        "city": config.author_city,
        "state": config.author_state,
        "zip": config.author_zip,
        "linkedin": config.author_linkedin,
        "website": config.author_website,
        "work_experience": [w.model_dump() for w in config.work_experience],
        "education": config.education.model_dump(),
        "certifications": config.certifications,
    }


@app.put("/api/agent/profile")
def agent_update_profile(data: ProfileUpdate, request: Request):
    """Agent-accessible profile update — uses X-API-Key auth."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    if not admin_id:
        raise HTTPException(503, "No admin user configured")
    config = storage.load_config(admin_id)
    if data.name: config.author_name = data.name
    if data.email: config.author_email = data.email
    if data.phone: config.author_phone = data.phone
    if data.location: config.author_location = data.location
    if data.address is not None: config.author_address = data.address
    if data.city is not None: config.author_city = data.city
    if data.state is not None: config.author_state = data.state
    if data.zip is not None: config.author_zip = data.zip
    if data.linkedin is not None: config.author_linkedin = data.linkedin
    if data.website is not None: config.author_website = data.website
    if data.work_experience is not None: config.work_experience = data.work_experience
    if data.education is not None: config.education = data.education
    if data.certifications is not None: config.certifications = data.certifications
    storage.save_config(admin_id, config)
    return agent_get_profile(request)


@app.get("/api/agent/key")
def get_agent_key(user: User = Depends(get_current_user)):
    """
    Returns the current agent API key. Requires OIDC session (admin only).
    Use this to retrieve the key to configure job-agent.
    """
    if not is_admin(user.id):
        raise HTTPException(403, "Admin only")
    return {"agent_api_key": get_agent_api_key()}


# ── Job CRUD ──────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(
    status: str | None = None,
    lane: str | None = None,
    min_score: int | None = None,
    has_docs: bool | None = None,
    ready_to_apply: bool | None = None,
    user: User = Depends(get_current_user),
):
    jobs = storage.load_all_jobs(user.id)
    if status:
        jobs = [j for j in jobs if j.status == status]
    if lane:
        jobs = [j for j in jobs if j.market_lane == lane]
    if min_score is not None:
        jobs = [j for j in jobs if j.score and j.score.total >= min_score]
    if has_docs is not None:
        jobs = [j for j in jobs if bool(j.tailored_resume_docx) == has_docs]
    if ready_to_apply is not None:
        jobs = [j for j in jobs if j.ready_to_apply == ready_to_apply]
    return [enrich_job(j) for j in jobs]


# ── Apply Queue (must be before /{job_id} to avoid route collision) ───────────

@app.get("/api/jobs/apply-queue")
def get_apply_queue(user: User = Depends(get_current_user)):
    """Pre-filtered list of jobs that are ready_to_apply, sorted by score desc."""
    jobs = storage.load_all_jobs(user.id)
    queue = [enrich_job(j) for j in jobs if j.ready_to_apply]
    queue.sort(key=lambda j: (j.get("score") or {}).get("total", 0), reverse=True)
    return {"count": len(queue), "jobs": queue}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return enrich_job(job)


@app.post("/api/jobs")
def create_job(data: JobCreate, user: User = Depends(get_current_user)):
    job = Job(
        raw_jd=data.raw_jd, title=data.title, company=data.company,
        url=data.url, source=data.source, pay_range=data.pay_range,
        notes=data.notes, intake_source=IntakeSource.MANUAL_PASTE,
    )
    storage.save_job(user.id, job)
    return enrich_job(job)


@app.post("/api/intake")
def intake_job(
    source: str = "manual_paste",
    raw_input: str = "",
    user: User = Depends(get_current_user),
):
    if not raw_input.strip():
        raise HTTPException(400, "No input provided")
    parsed = process_intake(source, raw_input)
    job = Job(
        raw_jd=parsed["raw_jd"], title=parsed.get("title", ""),
        company=parsed.get("company", ""), url=parsed.get("url", ""),
        source=parsed.get("source", source), pay_range=parsed.get("pay_range", ""),
        intake_source=IntakeSource(source) if source in IntakeSource._value2member_map_ else IntakeSource.MANUAL_PASTE,
    )
    try:
        meta = scoring.extract_job_metadata(job.raw_jd)
        if meta:
            if not job.title and meta.get("title"):
                job.title = meta["title"]
            if not job.company and meta.get("company"):
                job.company = meta["company"]
            if not job.pay_range and meta.get("pay_range"):
                job.pay_range = meta["pay_range"]
            if meta.get("is_contract"):
                job.market_lane = MarketLane.CONTRACT
    except Exception:
        pass
    storage.save_job(user.id, job)
    return enrich_job(job)


@app.post("/api/intake/linkedin-bulk")
def intake_linkedin_bulk(
    capture: linkedin_intake.LinkedInCapture,
    user: User = Depends(get_current_user),
):
    try:
        return linkedin_intake.process_linkedin_capture(capture, user.id)
    except Exception as e:
        raise HTTPException(500, f"LinkedIn intake failed: {str(e)}")


@app.post("/api/search")
def search_jobs(
    req: jobspy_search.JobSearchRequest,
    user: User = Depends(get_current_user),
):
    try:
        result = jobspy_search.run_search(req, user.id)
        return result.model_dump()
    except Exception as e:
        raise HTTPException(500, f"Search failed: {str(e)}")


# ── Batch scoring (before /{job_id} routes) ───────────────────────────────────

# ── Background scoring state ───────────────────────────────────────────────────

_score_task: dict = {
    "running": False,
    "user_id": None,
    "total": 0,
    "done": 0,
    "scored": 0,
    "errors": 0,
    "current": "",
    "error_msgs": [],
    "started_at": None,
    "finished_at": None,
}


def _run_score_all(user_id: str):
    import logging
    log = logging.getLogger(__name__)
    jobs = storage.load_all_jobs(user_id)
    to_score = [
        j for j in jobs
        if j.raw_jd.strip() and len(j.raw_jd) >= 200
        and (j.status == JobStatus.NEW or (j.score is not None and j.score.total == 0))
    ]
    _score_task.update(running=True, user_id=user_id, total=len(to_score),
                       done=0, scored=0, errors=0, current="", error_msgs=[],
                       started_at=datetime.now().isoformat(), finished_at=None)
    for job in to_score:
        if not _score_task["running"]:
            break
        _score_task["current"] = f"{job.company}: {job.title}"
        try:
            score_result = scoring.score_job(job, user_id)
            job.score = score_result
            job.update_status(JobStatus.SCORED)
            if score_result.recommended_lane:
                job.market_lane = score_result.recommended_lane
            storage.save_job(user_id, job)
            _score_task["scored"] += 1
        except Exception as e:
            log.error(f"score_all: failed {job.id} ({job.company}): {e}")
            _score_task["errors"] += 1
            _score_task["error_msgs"].append(f"{job.company}: {e}")
        _score_task["done"] += 1
    _score_task.update(running=False, current="", finished_at=datetime.now().isoformat())


@app.post("/api/jobs/score-all")
async def score_all_jobs(user: User = Depends(get_current_user)):
    """Kick off background scoring of all new/unscored jobs."""
    if _score_task["running"]:
        raise HTTPException(409, "Scoring already in progress")
    asyncio.get_event_loop().run_in_executor(None, _run_score_all, user.id)
    return {"started": True}


@app.post("/api/jobs/score-all/cancel")
async def cancel_score_all(user: User = Depends(get_current_user)):
    _score_task["running"] = False
    return {"cancelled": True}


@app.get("/api/jobs/score-all/status")
async def score_all_status(user: User = Depends(get_current_user)):
    return dict(_score_task)


@app.post("/api/jobs/apply-deal-breakers")
async def apply_deal_breakers(user: User = Depends(get_current_user)):
    """Re-apply deal breaker overrides to all scored jobs without re-calling the LLM."""
    config = storage.load_config(user.id)
    jobs = storage.load_all_jobs(user.id)
    fixed = []
    for job in jobs:
        if not job.score:
            continue
        old_total = job.score.total
        old_lane = job.score.recommended_lane
        scoring._apply_deal_breaker_override(job.score, job.raw_jd, config.deal_breakers)
        if job.score.total != old_total or job.score.recommended_lane != old_lane:
            job.score.total = job.score.skills_match + job.score.scope_impact + job.score.pay_alignment
            if job.score.recommended_lane.value == "ignore":
                job.market_lane = MarketLane.IGNORE
            storage.save_job(user.id, job)
            fixed.append({"id": job.id, "company": job.company, "title": job.title,
                          "old_score": old_total, "new_score": job.score.total})
    return {"fixed": len(fixed), "jobs": fixed}


@app.post("/api/jobs/score-batch")
async def score_batch(request: Request, user: User = Depends(get_current_user)):
    body = await request.json()
    job_ids = body if isinstance(body, list) else body.get("job_ids", [])
    results = []
    for jid in job_ids:
        job = storage.load_job(user.id, jid)
        if not job or not job.raw_jd.strip() or len(job.raw_jd) < 200:
            results.append({"id": jid, "status": "skipped", "reason": "no description"})
            continue
        try:
            score_result = scoring.score_job(job, user.id)
            job.score = score_result
            job.update_status(JobStatus.SCORED)
            if score_result.recommended_lane:
                job.market_lane = score_result.recommended_lane
            storage.save_job(user.id, job)
            results.append({"id": jid, "status": "scored", "total": score_result.total})
        except Exception as e:
            results.append({"id": jid, "status": "error", "error": str(e)})
    return {"results": results}


# ── Batch apply (before /{job_id} routes) ─────────────────────────────────────

@app.post("/api/jobs/apply-batch")
async def apply_batch(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Mark multiple jobs as applied in one call."""
    body = await request.json()
    job_ids = body.get("job_ids", [])
    applied_at = body.get("applied_at", "")

    if not isinstance(job_ids, list):
        raise HTTPException(400, "job_ids must be a list")

    results = []
    for jid in job_ids:
        job = storage.load_job(user.id, jid)
        if not job:
            results.append({"id": jid, "status": "not_found"})
            continue
        job.update_status(JobStatus.APPLIED, applied_date=applied_at)
        storage.save_job(user.id, job)
        results.append({"id": jid, "status": "applied", "company": job.company, "title": job.title})
    return {"results": results}


@app.post("/api/jobs/send-scored-digest")
def send_scored_digest(request: Request):
    """Email digest of pending-review jobs scored >= threshold. Accepts agent API key or user session."""
    key = request.headers.get("X-API-Key", "")
    if key and verify_agent_api_key(key):
        admin_id = get_admin_user_id()
    else:
        raise HTTPException(401, "Authentication required — use X-API-Key header")

    config = storage.load_config(admin_id)
    if not config.smtp_user or not config.smtp_password:
        raise HTTPException(400, "SMTP not configured")

    jobs = storage.load_all_jobs(admin_id)
    threshold = config.auto_generate_threshold or 7
    eligible = [
        j for j in jobs
        if j.score and j.score.total >= threshold
        and j.review_status == ReviewStatus.PENDING
        and j.status not in (JobStatus.APPLIED, JobStatus.REJECTED, JobStatus.PASSED)
    ]
    eligible.sort(key=lambda j: j.score.total, reverse=True)

    if not eligible:
        return {"sent": False, "reason": "No pending high-score jobs", "count": 0}

    JSHQ_URL = "http://jobsearch.lightbulbfan.duckdns.org"

    # ── Plain-text fallback ───────────────────────────────────────────────────
    lines = [f"Job Search HQ: {len(eligible)} job(s) scored >={threshold} awaiting review", ""]
    for j in eligible:
        score_str = f"{j.score.total}/10" if j.score else "?"
        pay = j.pay_range if j.pay_range and "nan" not in j.pay_range.lower() else ""
        lines.append(f"[{score_str}] {j.title} @ {j.company}")
        if pay:
            lines.append(f"  Pay: {pay}")
        if j.score and j.score.raw_analysis:
            lines.append(f"  Why: {j.score.raw_analysis[:200]}")
        lines.append(f"  Open: {JSHQ_URL}")
        lines.append("")
    lines.append(f"Review all: {JSHQ_URL}")
    plain_body = "\n".join(lines)

    # ── HTML body ─────────────────────────────────────────────────────────────
    def score_color(total):
        if total >= 9: return "#34d399"   # green
        if total >= 8: return "#4f8ff7"   # accent blue
        if total >= 7: return "#fbbf24"   # yellow
        return "#8b8fa3"

    cards_html = []
    for j in eligible:
        total = j.score.total if j.score else 0
        pay = j.pay_range if j.pay_range and "nan" not in j.pay_range.lower() else ""
        analysis = (j.score.raw_analysis[:280] + "...") if j.score and j.score.raw_analysis else ""
        sc = score_color(total)
        cards_html.append(f"""
        <div style="background:#1a1d27;border:1px solid #2e3347;border-radius:8px;padding:16px 20px;margin-bottom:12px;">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
            <span style="background:{sc};color:#0f1117;font-weight:700;font-size:15px;border-radius:6px;padding:2px 10px;white-space:nowrap;">{total}/10</span>
            <span style="color:#e1e4ed;font-size:16px;font-weight:600;">{j.title}</span>
          </div>
          <div style="color:#8b8fa3;font-size:13px;margin-bottom:6px;">{j.company}</div>
          {"<div style='color:#4f8ff7;font-size:13px;margin-bottom:6px;'>💰 " + pay + "</div>" if pay else ""}
          {"<div style='color:#c4c8d8;font-size:13px;line-height:1.5;margin-bottom:10px;'>" + analysis + "</div>" if analysis else ""}
          <a href="{JSHQ_URL}" style="display:inline-block;background:rgba(79,143,247,.15);color:#4f8ff7;border:1px solid rgba(79,143,247,.3);border-radius:6px;padding:5px 14px;font-size:12px;text-decoration:none;">Open in JSHQ →</a>
        </div>""")

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:680px;margin:0 auto;padding:24px 16px;">

    <div style="display:flex;align-items:center;gap:10px;margin-bottom:24px;">
      <span style="color:#4f8ff7;font-size:22px;font-weight:700;">JOB SEARCH HQ</span>
    </div>

    <div style="background:#1a1d27;border:1px solid #2e3347;border-radius:10px;padding:20px;margin-bottom:20px;">
      <div style="color:#e1e4ed;font-size:18px;font-weight:600;margin-bottom:4px;">
        {len(eligible)} job{'s' if len(eligible) != 1 else ''} ready for review
      </div>
      <div style="color:#8b8fa3;font-size:13px;">Scored &ge;{threshold}/10 &middot; Pending your go/no-go</div>
    </div>

    {"".join(cards_html)}

    <div style="text-align:center;margin-top:24px;">
      <a href="{JSHQ_URL}" style="display:inline-block;background:#4f8ff7;color:#fff;border-radius:8px;padding:12px 32px;font-size:15px;font-weight:600;text-decoration:none;">Open Job Search HQ</a>
    </div>

    <div style="color:#4a4f68;font-size:11px;text-align:center;margin-top:20px;">
      Sent by your homelab automation &middot; Job Search HQ
    </div>
  </div>
</body>
</html>"""

    to_addr = config.follow_up_email or config.smtp_user
    email_service.send_email(
        config=config,
        to=to_addr,
        subject=f"JSHQ: {len(eligible)} job(s) ready for review",
        body=plain_body,
        html_body=html_body if config.email_html else "",
    )
    return {"sent": True, "count": len(eligible), "to": to_addr}


@app.patch("/api/jobs/{job_id}")
def update_job(job_id: str, data: JobUpdate, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if data.title is not None: job.title = data.title
    if data.company is not None: job.company = data.company
    if data.url is not None: job.url = data.url
    if data.pay_range is not None: job.pay_range = data.pay_range
    if data.market_lane is not None: job.market_lane = data.market_lane
    if data.notes is not None: job.notes = data.notes
    if data.status is not None:
        job.update_status(data.status, applied_date=data.applied_at or "")
    elif data.applied_at is not None:
        job.applied_at = data.applied_at
        if not job.follow_up.due_at:
            try:
                dt = datetime.fromisoformat(data.applied_at)
                job.follow_up.due_at = (dt + timedelta(days=14)).isoformat()
            except ValueError:
                pass
    if data.gut_interest is not None and job.score:
        job.score.gut_interest = data.gut_interest
        job.score.total = (
            job.score.skills_match + job.score.scope_impact
            + job.score.pay_alignment + job.score.gut_interest
        )
    job.updated_at = datetime.now().isoformat()
    storage.save_job(user.id, job)
    return enrich_job(job)


@app.get("/api/intake/debug-url")
def debug_url_intake(url: str, user: User = Depends(get_current_user)):
    """Show what the server/container can extract from a job URL."""
    if not url.strip():
        raise HTTPException(400, "No URL provided")
    try:
        job, attempts = company_site_search._fetch_best_job_page_with_diagnostics(
            url,
            company_site_search._company_from_host(url),
        )
        return {
            "url": url,
            "selected": {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "pay_range": job.pay_range,
                "raw_jd_length": len(job.raw_jd),
                "has_real_job_description": company_site_search._has_real_job_description(job.raw_jd),
                "extraction_method": job.extraction_method,
                "preview": job.raw_jd[:800],
            },
            "attempts": [a.__dict__ for a in attempts],
        }
    except Exception as e:
        raise HTTPException(500, f"Debug scrape failed: {str(e)}")


@app.post("/api/jobs/{job_id}/refresh-from-url")
def refresh_job_from_url(job_id: str, user: User = Depends(get_current_user)):
    """Re-scrape a saved job URL and replace stale placeholder JD text."""
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.url.strip():
        raise HTTPException(400, "Job has no URL to refresh")

    parsed = process_intake("url_scrape", job.url)
    raw_jd = parsed.get("raw_jd", "").strip()
    if not raw_jd:
        raise HTTPException(502, "URL refresh returned no job description")

    job.raw_jd = raw_jd
    if parsed.get("title"):
        job.title = parsed["title"]
    if parsed.get("company"):
        job.company = parsed["company"]
    if parsed.get("pay_range"):
        job.pay_range = parsed["pay_range"]
    if parsed.get("url"):
        job.url = parsed["url"]
    job.source = parsed.get("source", job.source) or job.source
    job.updated_at = datetime.now().isoformat()
    storage.save_job(user.id, job)
    return enrich_job(job)


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str, user: User = Depends(get_current_user)):
    if not storage.delete_job(user.id, job_id):
        raise HTTPException(404, "Job not found")
    return {"ok": True}


# ── Scoring ───────────────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/score")
def score_job(job_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.raw_jd.strip():
        raise HTTPException(400, "No JD text to score")
    try:
        result = scoring.score_job(job, user.id)
        job.score = result
        job.update_status(JobStatus.SCORED)
        if result.recommended_lane:
            job.market_lane = result.recommended_lane
        storage.save_job(user.id, job)
        enriched = enrich_job(job)
        enriched["scored_with"] = ai_router.get_last_model("fast")
        return enriched
    except Exception as e:
        raise HTTPException(500, f"Scoring failed: {str(e)}")


# ── Resume Generation ─────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/tailored-resume")
def generate_tailored_resume(job_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.score:
        raise HTTPException(400, "Job must be scored first")
    try:
        resume_text = scoring.generate_tailored_resume(job, user.id)
        job.tailored_resume = resume_text
        docx_path = docx_builder.generate_resume_docx(
            resume_text, job.id, user.id, job.company, job.title
        )
        job.tailored_resume_docx = docx_path
        job.updated_at = datetime.now().isoformat()
        storage.save_job(user.id, job)
        return {
            "tailored_resume": resume_text,
            "docx_path": docx_path,
            "docx_filename": Path(docx_path).name,
        }
    except Exception as e:
        raise HTTPException(500, f"Resume generation failed: {str(e)}")


@app.get("/api/jobs/{job_id}/download-resume")
def download_resume(job_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.tailored_resume_docx or not Path(job.tailored_resume_docx).exists():
        raise HTTPException(404, "No resume .docx generated yet")
    return FileResponse(
        job.tailored_resume_docx,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=Path(job.tailored_resume_docx).name,
    )


# ── Cover Letters ─────────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/cover-letters")
def generate_cover_letters(job_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.score:
        raise HTTPException(400, "Job must be scored first")
    try:
        config = storage.load_config(user.id)
        letters = scoring.generate_cover_letters(job, user.id)
        for letter in letters:
            docx_path = docx_builder.generate_cover_letter_docx(
                content=letter.content,
                variant=letter.variant,
                job_id=job.id,
                user_id=user.id,
                company=job.company,
                title=job.title,
                author_name=config.author_name,
                author_location=config.author_location,
                author_phone=config.author_phone,
                author_email=config.smtp_user or config.follow_up_email,
            )
            letter.docx_path = docx_path
        job.cover_letters = letters
        job.updated_at = datetime.now().isoformat()
        storage.save_job(user.id, job)
        return [l.model_dump() for l in letters]
    except Exception as e:
        raise HTTPException(500, f"Cover letter generation failed: {str(e)}")


@app.get("/api/jobs/{job_id}/cover-letters/{letter_id}/download")
def download_cover_letter(job_id: str, letter_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    letter = next((cl for cl in job.cover_letters if cl.id == letter_id), None)
    if not letter:
        raise HTTPException(404, "Cover letter not found")
    if not letter.docx_path or not Path(letter.docx_path).exists():
        raise HTTPException(404, "No .docx generated for this cover letter")
    return FileResponse(
        letter.docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=Path(letter.docx_path).name,
    )


# ── Generate Docs (resume + cover letters in one call) ────────────────────────

@app.post("/api/jobs/{job_id}/generate-docs")
def generate_docs(job_id: str, user: User = Depends(get_current_user)):
    """Generate tailored resume + all 3 cover letter variants in a single call."""
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.score:
        raise HTTPException(400, "Job must be scored first")
    config = storage.load_config(user.id)
    errors = []

    # Resume
    try:
        resume_text = scoring.generate_tailored_resume(job, user.id)
        job.tailored_resume = resume_text
        docx_path = docx_builder.generate_resume_docx(
            resume_text, job.id, user.id, job.company, job.title
        )
        job.tailored_resume_docx = docx_path
    except Exception as e:
        errors.append(f"Resume: {str(e)}")

    # Cover letters
    try:
        letters = scoring.generate_cover_letters(job, user.id)
        for letter in letters:
            docx_path = docx_builder.generate_cover_letter_docx(
                content=letter.content,
                variant=letter.variant,
                job_id=job.id,
                user_id=user.id,
                company=job.company,
                title=job.title,
                author_name=config.author_name,
                author_location=config.author_location,
                author_phone=config.author_phone,
                author_email=config.smtp_user or config.follow_up_email,
            )
            letter.docx_path = docx_path
        job.cover_letters = letters
    except Exception as e:
        errors.append(f"Cover letters: {str(e)}")

    job.updated_at = datetime.now().isoformat()
    storage.save_job(user.id, job)
    result = enrich_job(job)
    result["errors"] = errors
    return result


# ── Lightweight status check ──────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/status")
def get_job_status(job_id: str, user: User = Depends(get_current_user)):
    """Lightweight status snapshot — no full JD/resume text, fast to call."""
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "status": job.status,
        "market_lane": job.market_lane,
        "score": job.score.total if job.score else None,
        "has_resume": bool(job.tailored_resume_docx),
        "has_cover_letters": len(job.cover_letters) > 0,
        "ready_to_apply": job.ready_to_apply,
        "applied_at": job.applied_at,
        "follow_up_due": job.follow_up_due,
        "url": job.url,
    }


# ── Application package (full payload for browser automation) ─────────────────

@app.get("/api/jobs/{job_id}/application-package")
def get_application_package(job_id: str, user: User = Depends(get_current_user)):
    """Everything Claude needs to fill out an application form in one payload."""
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    config = storage.load_config(user.id)
    return {
        "job": enrich_job(job),
        "profile": {
            "name": config.author_name,
            "email": config.author_email or config.smtp_user or config.follow_up_email,
            "phone": config.author_phone,
            "location": config.author_location,
        },
        "resume_download_url": f"/api/jobs/{job_id}/download-resume" if job.tailored_resume_docx else None,
        "resume_text": job.tailored_resume,
        "cover_letter_direct": next(
            (cl.content for cl in job.cover_letters if cl.variant == "direct"), None
        ),
        "cover_letter_brief": next(
            (cl.content for cl in job.cover_letters if cl.variant == "brief"), None
        ),
        "ready_to_apply": job.ready_to_apply,
    }


# ── Application result write-back ─────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/application-result")
def record_application_result(
    job_id: str,
    result: ApplicationResult,
    user: User = Depends(get_current_user),
):
    """Called by Claude after attempting to submit an application."""
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.update_status(result.status, applied_date=result.applied_at)
    if result.notes:
        job.notes = (job.notes + "\n" + result.notes).strip() if job.notes else result.notes
    if result.portal_url:
        portal_note = f"Portal: {result.portal_url}"
        job.follow_up.notes = (
            job.follow_up.notes + "\n" + portal_note
        ).strip() if job.follow_up.notes else portal_note
    if result.error:
        job.notes = (job.notes + f"\nAutomation error: {result.error}").strip()
    job.updated_at = datetime.now().isoformat()
    storage.save_job(user.id, job)
    return enrich_job(job)


# ── Agent: ATS URL + event log ────────────────────────────────────────────────

class AtsUrlPayload(BaseModel):
    ats_url: str


@app.get("/api/agent/jobs/{job_id}/package")
def get_job_package(job_id: str, request: Request):
    """Return job details + download URLs for resume and cover letter. Called by dispatch.js."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    base = f"/api/agent/jobs/{job_id}"
    return {
        "job": {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "url": job.url,
            "ats_url": job.ats_url,
            "score": job.score.total if job.score else None,
            "lane": job.market_lane,
            "recommended_resume": job.score.recommended_resume if job.score else "base",
        },
        "downloads": {
            "resume": f"{base}/resume/download",
            "cover_letter_direct": f"{base}/cover-letter/download?variant=direct",
            "cover_letter_brief": f"{base}/cover-letter/download?variant=brief",
        }
    }


@app.get("/api/agent/jobs/{job_id}/research")
def agent_get_job_research(job_id: str, request: Request):
    """Return existing company research (contacts, summary) for a job, if
    any has been run. Read-only - does NOT trigger a new (paid) research
    run. Called by company-intel."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": job_id,
        "company": job.company,
        "title": job.title,
        "research": job.research,
    }


@app.post("/api/agent/jobs/{job_id}/research")
def agent_trigger_job_research(job_id: str, request: Request):
    """Trigger a NEW company research run for a job (uses the paid Claude
    web-search API, same as the session-authenticated /api/jobs/{id}/research
    route). Deliberately a separate opt-in endpoint from the GET above, so
    an agent reading job data doesn't silently incur API cost. Called by
    company-intel."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.company or job.company.lower() in ("nan", "unknown", ""):
        raise HTTPException(400, "Job has no company name to research")
    try:
        result = company_research.research_company(
            company=job.company, job_title=job.title, user_id=admin_id,
        )
        from models import CompanyResearch, Contact
        job.research = CompanyResearch(
            contacts=[Contact(**c) for c in result.get("contacts", [])],
            company_summary=result.get("company_summary", ""),
            researched_at=datetime.now().isoformat(),
            searches_run=result.get("searches_run", []),
        )
        job.updated_at = datetime.now().isoformat()
        storage.save_job(admin_id, job)
    except Exception as e:
        raise HTTPException(500, f"Research failed: {str(e)}")
    return {"job_id": job_id, "researched": True}


@app.get("/api/agent/jobs/{job_id}/resume/download")
def download_agent_resume(job_id: str, request: Request):
    """Download the tailored resume .docx for a job. Called by dispatch.js."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.tailored_resume_docx or not Path(job.tailored_resume_docx).exists():
        raise HTTPException(404, "No tailored resume found for this job")
    return FileResponse(job.tailored_resume_docx, filename=f"resume_{job.company}_{job.id}.docx")


@app.get("/api/agent/jobs/{job_id}/cover-letter/download")
def download_agent_cover_letter(job_id: str, variant: str = "direct", request: Request = None):
    """Download a cover letter .docx for a job. Called by dispatch.js."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    cl = next((c for c in job.cover_letters if c.variant == variant), None)
    if not cl or not cl.docx_path or not Path(cl.docx_path).exists():
        raise HTTPException(404, f"No cover letter variant '{variant}' found for this job")
    return FileResponse(cl.docx_path, filename=f"cover_letter_{variant}_{job.company}_{job.id}.docx")


@app.patch("/api/agent/jobs/{job_id}/ats-url")
def set_ats_url(job_id: str, payload: AtsUrlPayload, request: Request):
    """Save the resolved ATS URL for a job. Called by dispatch.js."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.ats_url = payload.ats_url
    job.updated_at = datetime.now().isoformat()
    storage.save_job(admin_id, job)
    return {"id": job_id, "ats_url": job.ats_url}


@app.post("/api/agent/jobs/{job_id}/log")
def append_agent_log(job_id: str, event: AgentEvent, request: Request):
    """Append an automation event to a job's agent_log. Called by Hermes/dispatch."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.agent_log.append(event)
    job.updated_at = datetime.now().isoformat()
    storage.save_job(admin_id, job)
    return {"id": job_id, "agent_log_count": len(job.agent_log)}


@app.get("/api/agent/jobs/{job_id}/log")
def get_agent_log(job_id: str, request: Request):
    """Return the full agent_log for a job."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    total_tokens_in  = sum(e.claude_tokens_in  for e in job.agent_log)
    total_tokens_out = sum(e.claude_tokens_out for e in job.agent_log)
    return {
        "job_id": job_id,
        "events": [e.model_dump() for e in job.agent_log],
        "totals": {
            "events": len(job.agent_log),
            "claude_tokens_in":  total_tokens_in,
            "claude_tokens_out": total_tokens_out,
            "claude_tokens_total": total_tokens_in + total_tokens_out,
        }
    }



@app.patch("/api/agent/jobs/{job_id}/review")
def agent_set_review_status(job_id: str, request: Request, body: dict):
    """Set review_status on a job (pending/approved/rejected). Agent-auth."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    status_val = body.get("review_status", "")
    try:
        new_status = ReviewStatus(status_val)
    except ValueError:
        raise HTTPException(400, f"Invalid review_status '{status_val}'. Use: pending, approved, rejected")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.review_status = new_status
    job.updated_at = datetime.now().isoformat()
    storage.save_job(admin_id, job)
    return {"job_id": job_id, "review_status": new_status}


@app.get("/api/agent/jobs/needs-refresh")
def agent_jobs_needing_refresh(request: Request, url_contains: str = "", limit: int = 500):
    """
    List pending jobs whose raw_jd is too thin to score (e.g. digest-sourced
    listings awaiting a full JD fetch). Optionally filter by URL substring
    (e.g. 'linkedin.com') so a single-site refresher only pulls its own jobs.
    """
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    admin_id = get_admin_user_id()
    jobs = storage.load_all_jobs(admin_id)
    candidates = [
        j for j in jobs
        if j.review_status == ReviewStatus.PENDING
        and not j.score
        and len((j.raw_jd or "").strip()) < 200
        and j.url
        and (url_contains in j.url if url_contains else True)
    ]
    candidates = candidates[:limit]
    return {
        "count": len(candidates),
        "jobs": [{"id": j.id, "url": j.url, "title": j.title, "company": j.company} for j in candidates],
    }


class RefreshAndScorePayload(BaseModel):
    raw_jd: str
    title: str = ""
    company: str = ""
    pay_range: str = ""


@app.post("/api/agent/jobs/{job_id}/refresh-and-score")
def agent_refresh_and_score(job_id: str, payload: RefreshAndScorePayload, request: Request):
    """Replace a job's raw_jd with a freshly-fetched full JD and score it. Agent-auth."""
    key = request.headers.get("X-API-Key", "")
    if not key or not verify_agent_api_key(key):
        raise HTTPException(401, "Invalid or missing API key")
    if not payload.raw_jd.strip():
        raise HTTPException(400, "raw_jd is required")
    admin_id = get_admin_user_id()
    job = storage.load_job(admin_id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    job.raw_jd = payload.raw_jd.strip()
    if payload.title:
        job.title = payload.title
    if payload.company:
        job.company = payload.company
    if payload.pay_range:
        job.pay_range = payload.pay_range
    job.updated_at = datetime.now().isoformat()

    try:
        result = scoring.score_job(job, admin_id)
        job.score = result
        job.update_status(JobStatus.SCORED)
        if result.recommended_lane:
            job.market_lane = result.recommended_lane
        storage.save_job(admin_id, job)
        return {"job_id": job_id, "total": result.total, "recommended_lane": job.market_lane}
    except Exception as e:
        storage.save_job(admin_id, job)
        raise HTTPException(500, f"Scoring failed after refresh: {str(e)}")


# ── Email ─────────────────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/send-email")
def send_job_email(job_id: str, data: EmailCompose, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    config = storage.load_config(user.id)

    attachments = []
    if data.attach_resume and job.tailored_resume_docx and Path(job.tailored_resume_docx).exists():
        attachments.append(job.tailored_resume_docx)

    try:
        result = email_service.send_email(
            config=config, to=data.to, subject=data.subject,
            body=data.body, cc=data.cc, attachments=attachments,
        )
        job.emails.append(EmailRecord(
            direction="sent", to=data.to, subject=data.subject,
            body=data.body, attachments=[Path(a).name for a in attachments],
        ))
        job.updated_at = datetime.now().isoformat()
        storage.save_job(user.id, job)
        return result
    except Exception as e:
        raise HTTPException(500, f"Email send failed: {str(e)}")


@app.post("/api/jobs/{job_id}/parse-confirmation")
async def parse_confirmation(job_id: str, request: Request, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    body = await request.json()
    raw_email = body.get("raw_email", "")
    if not raw_email.strip():
        raise HTTPException(400, "No email text provided")

    try:
        parsed = email_service.parse_confirmation_email(raw_email)

        if parsed.get("contact_email") and not parsed["contact_email"].startswith("noreply"):
            job.follow_up.contact_email = parsed["contact_email"]
        elif parsed.get("reply_to") and not parsed["reply_to"].startswith("noreply"):
            job.follow_up.contact_email = parsed["reply_to"]
        if not job.follow_up.contact_email and parsed.get("contact_emails"):
            job.follow_up.contact_email = parsed["contact_emails"][0]
        if parsed.get("contact_name"):
            job.follow_up.contact_name = parsed["contact_name"]
        if parsed.get("contact_phone"):
            job.follow_up.contact_phone = parsed["contact_phone"]
        if parsed.get("all_emails"):
            job.follow_up.extracted_emails = parsed["all_emails"]
        if parsed.get("all_urls"):
            job.follow_up.extracted_links = parsed["all_urls"]

        notes_parts = []
        if parsed.get("next_steps"):
            notes_parts.append(parsed["next_steps"])
        if parsed.get("portal_url"):
            notes_parts.append(f"Portal: {parsed['portal_url']}")
        if notes_parts:
            job.follow_up.notes = " | ".join(notes_parts)

        job.emails.append(EmailRecord(
            direction="received", body=raw_email[:2000],
            subject=f"Confirmation from {job.company}",
        ))

        if parsed.get("is_interview_request") and job.status == JobStatus.APPLIED:
            job.update_status(JobStatus.INTERVIEW)
        elif parsed.get("is_rejection"):
            job.update_status(JobStatus.REJECTED)

        job.updated_at = datetime.now().isoformat()
        storage.save_job(user.id, job)
        return {"parsed": parsed, "job": enrich_job(job)}
    except Exception as e:
        raise HTTPException(500, f"Parsing failed: {str(e)}")


# ── Follow-Up Tracking ────────────────────────────────────────────────────────

@app.get("/api/follow-ups")
def get_follow_ups(user: User = Depends(get_current_user)):
    jobs = storage.load_all_jobs(user.id)
    due = [enrich_job(j) for j in jobs if j.follow_up_due]
    upcoming = [
        enrich_job(j) for j in jobs
        if j.status in (JobStatus.APPLIED, JobStatus.INTERVIEW)
        and not j.follow_up_due and j.follow_up.due_at
    ]
    return {
        "due_now": due,
        "upcoming": upcoming[:10],
        "total_applied": len([j for j in jobs if j.status == JobStatus.APPLIED]),
    }


@app.post("/api/jobs/{job_id}/snooze-follow-up")
def snooze_follow_up(job_id: str, days: int = 7, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.follow_up.due_at = (datetime.now() + timedelta(days=days)).isoformat()
    job.updated_at = datetime.now().isoformat()
    storage.save_job(user.id, job)
    return enrich_job(job)


@app.post("/api/jobs/{job_id}/set-contact")
def set_contact_email(
    job_id: str, email: str = "", name: str = "",
    user: User = Depends(get_current_user),
):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if email:
        job.follow_up.contact_email = email
    if name:
        job.follow_up.contact_name = name
    job.updated_at = datetime.now().isoformat()
    storage.save_job(user.id, job)
    return enrich_job(job)


@app.post("/api/jobs/{job_id}/mark-followed-up")
def mark_followed_up(job_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    config = storage.load_config(user.id)
    job.follow_up.count += 1
    job.follow_up.due_at = (datetime.now() + timedelta(days=config.follow_up_days)).isoformat()
    job.updated_at = datetime.now().isoformat()
    storage.save_job(user.id, job)
    return enrich_job(job)


# ── Company Research ───────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/research")
def research_job_company(job_id: str, user: User = Depends(get_current_user)):
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.company or job.company.lower() in ("nan", "unknown", ""):
        raise HTTPException(400, "Job has no company name to research")
    try:
        result = company_research.research_company(
            company=job.company,
            job_title=job.title,
            user_id=user.id,
        )
        from models import CompanyResearch, Contact
        job.research = CompanyResearch(
            contacts=[Contact(**c) for c in result.get("contacts", [])],
            company_summary=result.get("company_summary", ""),
            researched_at=datetime.now().isoformat(),
            searches_run=result.get("searches_run", []),
        )
        job.updated_at = datetime.now().isoformat()
        storage.save_job(user.id, job)
        return enrich_job(job)
    except Exception as e:
        raise HTTPException(500, f"Research failed: {str(e)}")


class ContactMessageRequest(BaseModel):
    contact_name: str
    contact_title: str


@app.post("/api/jobs/{job_id}/contact-message")
def generate_contact_message(
    job_id: str,
    req: ContactMessageRequest,
    user: User = Depends(get_current_user),
):
    """Generate a 300-char LinkedIn connection message for a specific contact."""
    job = storage.load_job(user.id, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    config = storage.load_config(user.id)
    try:
        message = scoring.generate_linkedin_message(
            job=job,
            contact_name=req.contact_name,
            contact_title=req.contact_title,
            author_name=config.author_name or "Salil",
            user_id=user.id,
        )
        return {"message": message}
    except Exception as e:
        raise HTTPException(500, f"Message generation failed: {str(e)}")


@app.post("/api/follow-ups/send-digest")
def send_follow_up_digest(user: User = Depends(get_current_user)):
    config = storage.load_config(user.id)
    jobs = storage.load_all_jobs(user.id)
    due_jobs = [j for j in jobs if j.follow_up_due]
    try:
        return email_service.send_follow_up_digest(config, due_jobs)
    except Exception as e:
        raise HTTPException(500, f"Email send failed: {str(e)}")




# ── Config, Profile & Resumes ─────────────────────────────────────────────────

@app.post("/api/test-email")
def test_email(user: User = Depends(get_current_user)):
    config = storage.load_config(user.id)
    if not config.smtp_user or not config.smtp_password:
        raise HTTPException(400, "SMTP not configured.")
    try:
        email_service.send_email(
            config=config,
            to=config.follow_up_email or config.smtp_user,
            subject="Job Search HQ — Test Email",
            body="This is a test email from Job Search HQ.\n\nSMTP is working correctly.",
        )
        return {"sent": True}
    except Exception as e:
        raise HTTPException(500, f"SMTP test failed: {str(e)}")


@app.get("/api/config")
def get_config(user: User = Depends(get_current_user)):
    return storage.load_config(user.id).model_dump()


@app.put("/api/config")
def update_config(config: AppConfig, user: User = Depends(get_current_user)):
    # Preserve profile fields from existing config if not provided in payload
    existing = storage.load_config(user.id)
    if not config.author_name:    config.author_name    = existing.author_name
    if not config.author_email:   config.author_email   = existing.author_email
    if not config.author_phone:   config.author_phone   = existing.author_phone
    if not config.author_location: config.author_location = existing.author_location
    storage.save_config(user.id, config)
    scheduler.update_schedule(config, user.id)
    return config.model_dump()


@app.get("/api/profile")
def get_profile(user: User = Depends(get_current_user)):
    config = storage.load_config(user.id)
    return {
        "name": config.author_name,
        "email": config.author_email or config.smtp_user or config.follow_up_email,
        "phone": config.author_phone,
        "location": config.author_location,
        "address": config.author_address,
        "city": config.author_city,
        "state": config.author_state,
        "zip": config.author_zip,
        "linkedin": config.author_linkedin,
        "website": config.author_website,
        "work_experience": [w.model_dump() for w in config.work_experience],
        "education": config.education.model_dump(),
        "certifications": config.certifications,
    }


@app.put("/api/profile")
def update_profile(data: ProfileUpdate, user: User = Depends(get_current_user)):
    config = storage.load_config(user.id)
    if data.name: config.author_name = data.name
    if data.email: config.author_email = data.email
    if data.phone: config.author_phone = data.phone
    if data.location: config.author_location = data.location
    if data.address is not None: config.author_address = data.address
    if data.city is not None: config.author_city = data.city
    if data.state is not None: config.author_state = data.state
    if data.zip is not None: config.author_zip = data.zip
    if data.linkedin is not None: config.author_linkedin = data.linkedin
    if data.website is not None: config.author_website = data.website
    if data.work_experience is not None: config.work_experience = data.work_experience
    if data.education is not None: config.education = data.education
    if data.certifications is not None: config.certifications = data.certifications
    storage.save_config(user.id, config)
    return get_profile(user)


@app.get("/api/resumes")
def list_resumes(user: User = Depends(get_current_user)):
    variants = ["director", "base", "contract", "full_history"]
    result = {}
    for v in variants:
        text = storage.load_resume_text(user.id, v)
        result[v] = {
            "loaded": bool(text),
            "length": len(text),
            "preview": text[:200] + "..." if len(text) > 200 else text,
        }
    return result


@app.put("/api/resumes/{variant}")
async def upload_resume(variant: str, request: Request, user: User = Depends(get_current_user)):
    if variant not in ["director", "base", "contract", "full_history"]:
        raise HTTPException(400, "Invalid variant")
    body = await request.json()
    content = body.get("content", "")
    storage.save_resume_text(user.id, variant, content)
    return {"ok": True, "variant": variant, "length": len(content)}


@app.get("/api/resumes/{variant}/text")
def get_resume_text(variant: str, user: User = Depends(get_current_user)):
    """Get full resume text for a variant."""
    if variant not in ["director", "base", "contract", "full_history"]:
        raise HTTPException(400, "Invalid variant")
    text = storage.load_resume_text(user.id, variant)
    return {"variant": variant, "content": text, "length": len(text)}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def dashboard_stats(user: User = Depends(get_current_user)):
    jobs = storage.load_all_jobs(user.id)
    by_status, by_lane, scores, follow_ups_due = {}, {}, [], 0
    ready_count = 0
    for j in jobs:
        by_status[j.status] = by_status.get(j.status, 0) + 1
        by_lane[j.market_lane] = by_lane.get(j.market_lane, 0) + 1
        if j.score:
            scores.append(j.score.total)
        if j.follow_up_due:
            follow_ups_due += 1
        if j.ready_to_apply:
            ready_count += 1
    return {
        "total": len(jobs),
        "by_status": by_status,
        "by_lane": by_lane,
        "follow_ups_due": follow_ups_due,
        "ready_to_apply": ready_count,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "score_distribution": {
            "high_8_10": len([s for s in scores if s >= 8]),
            "mid_5_7": len([s for s in scores if 5 <= s <= 7]),
            "low_0_4": len([s for s in scores if s < 5]),
        },
    }


# ── Scheduler / Automation ────────────────────────────────────────────────────

@app.get("/api/scheduler/status")
def scheduler_status(user: User = Depends(get_current_user)):
    """Get current scheduler status, next run, and config."""
    return scheduler.get_schedule_status(user.id)


@app.post("/api/scheduler/run-now")
def scheduler_run_now(user: User = Depends(get_current_user)):
    """Manually trigger the daily search pipeline."""
    try:
        result = scheduler.run_pipeline_now(user.id)
        return result
    except Exception as e:
        raise HTTPException(500, f"Pipeline failed: {str(e)}")


@app.get("/api/scheduler/logs")
def scheduler_logs(count: int = 10, user: User = Depends(get_current_user)):
    """Get recent scheduler run logs."""
    return scheduler.get_recent_logs(count)


# ── Static & root ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", 8094))
    uvicorn.run(app, host="0.0.0.0", port=port)
