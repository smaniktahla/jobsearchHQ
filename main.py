import os
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import (
    get_current_user,
    invalidate_discovery_cache,
    is_setup_complete,
    load_system_config,
    login_handler,
    callback_handler,
    logout_handler,
    save_system_config,
)
from pydantic import BaseModel
from models import (
    AppConfig, EmailCompose, EmailRecord, IntakeSource, Job,
    JobCreate, JobStatus, JobUpdate, MarketLane, User,
)
import docx_builder
import email_service
import jobspy_search
import linkedin_intake
import scoring
import storage
import company_research
import ai_router
import scheduler
from intake import process_intake

app = FastAPI(title="Job Search HQ", version="2.1")


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
    return {"id": session["user_id"], "email": session["email"], "name": session["name"]}


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


# ── Job CRUD ──────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(
    status: str | None = None,
    lane: str | None = None,
    user: User = Depends(get_current_user),
):
    jobs = storage.load_all_jobs(user.id)
    if status:
        jobs = [j for j in jobs if j.status == status]
    if lane:
        jobs = [j for j in jobs if j.market_lane == lane]
    return [enrich_job(j) for j in jobs]


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


@app.post("/api/jobs/score-batch")
def score_batch(job_ids: list[str] = [], user: User = Depends(get_current_user)):
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
    job.follow_up.sent_at = datetime.now().isoformat()
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


# ── Config & Resumes ──────────────────────────────────────────────────────────

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
    storage.save_config(user.id, config)
    scheduler.update_schedule(config, user.id)
    return config.model_dump()


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
    for j in jobs:
        by_status[j.status] = by_status.get(j.status, 0) + 1
        by_lane[j.market_lane] = by_lane.get(j.market_lane, 0) + 1
        if j.score:
            scores.append(j.score.total)
        if j.follow_up_due:
            follow_ups_due += 1
    return {
        "total": len(jobs),
        "by_status": by_status,
        "by_lane": by_lane,
        "follow_ups_due": follow_ups_due,
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
