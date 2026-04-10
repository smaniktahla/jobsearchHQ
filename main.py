import os
from datetime import datetime, timedelta
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

from models import (
    Job, JobCreate, JobUpdate, JobStatus, MarketLane,
    IntakeSource, AppConfig, ScoreBreakdown, FollowUp,
    EmailCompose, EmailRecord
)
import storage
import scoring
import email_service
import docx_builder
import linkedin_intake
import jobspy_search
from intake import process_intake

app = FastAPI(title="Job Search Command Center", version="2.0")


# === Helpers ===

def enrich_job(job: Job) -> dict:
    d = job.model_dump()
    d["follow_up_due"] = job.follow_up_due
    d["days_since_applied"] = job.days_since_applied
    return d


# === Job CRUD ===

@app.get("/api/jobs")
def list_jobs(status: str | None = None, lane: str | None = None):
    jobs = storage.load_all_jobs()
    if status:
        jobs = [j for j in jobs if j.status == status]
    if lane:
        jobs = [j for j in jobs if j.market_lane == lane]
    return [enrich_job(j) for j in jobs]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return enrich_job(job)


@app.post("/api/jobs")
def create_job(data: JobCreate):
    job = Job(
        raw_jd=data.raw_jd, title=data.title, company=data.company,
        url=data.url, source=data.source, pay_range=data.pay_range,
        notes=data.notes, intake_source=IntakeSource.MANUAL_PASTE,
    )
    storage.save_job(job)
    return enrich_job(job)


@app.post("/api/intake")
def intake_job(source: str = "manual_paste", raw_input: str = ""):
    if not raw_input.strip():
        raise HTTPException(400, "No input provided")
    parsed = process_intake(source, raw_input)
    job = Job(
        raw_jd=parsed["raw_jd"], title=parsed.get("title", ""),
        company=parsed.get("company", ""), url=parsed.get("url", ""),
        source=parsed.get("source", source), pay_range=parsed.get("pay_range", ""),
        intake_source=IntakeSource(source) if source in IntakeSource.__members__.values() else IntakeSource.MANUAL_PASTE,
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
    storage.save_job(job)
    return enrich_job(job)


@app.post("/api/intake/linkedin-bulk")
def intake_linkedin_bulk(capture: linkedin_intake.LinkedInCapture):
    """Process a LinkedIn page capture from the browser extension."""
    try:
        return linkedin_intake.process_linkedin_capture(capture)
    except Exception as e:
        raise HTTPException(500, f"LinkedIn intake failed: {str(e)}")


# === Job Board Search (JobSpy) ===

@app.post("/api/search")
def search_jobs(req: jobspy_search.JobSearchRequest):
    """Search multiple job boards via JobSpy and import results."""
    try:
        result = jobspy_search.run_search(req)
        return result.model_dump()
    except Exception as e:
        raise HTTPException(500, f"Search failed: {str(e)}")


@app.post("/api/jobs/score-batch")
def score_batch(job_ids: list[str] = []):
    """Score multiple jobs in batch. Used after a search import."""
    results = []
    for jid in job_ids:
        job = storage.load_job(jid)
        if not job or not job.raw_jd.strip() or len(job.raw_jd) < 200:
            results.append({"id": jid, "status": "skipped", "reason": "no description"})
            continue
        try:
            score_result = scoring.score_job(job)
            job.score = score_result
            job.update_status(JobStatus.SCORED)
            if score_result.recommended_lane:
                job.market_lane = score_result.recommended_lane
            storage.save_job(job)
            results.append({"id": jid, "status": "scored", "total": score_result.total})
        except Exception as e:
            results.append({"id": jid, "status": "error", "error": str(e)})
    return {"results": results}


@app.patch("/api/jobs/{job_id}")
def update_job(job_id: str, data: JobUpdate):
    job = storage.load_job(job_id)
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
        # Allow updating applied_at without changing status
        job.applied_at = data.applied_at
        if not job.follow_up.due_at:
            try:
                dt = datetime.fromisoformat(data.applied_at)
                job.follow_up.due_at = (dt + timedelta(days=14)).isoformat()
            except ValueError:
                pass
    if data.gut_interest is not None and job.score:
        job.score.gut_interest = data.gut_interest
        job.score.total = job.score.skills_match + job.score.scope_impact + job.score.pay_alignment + job.score.gut_interest
    job.updated_at = datetime.now().isoformat()
    storage.save_job(job)
    return enrich_job(job)


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str):
    if not storage.delete_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"ok": True}


# === Scoring ===

@app.post("/api/jobs/{job_id}/score")
def score_job(job_id: str):
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.raw_jd.strip():
        raise HTTPException(400, "No JD text to score")
    try:
        result = scoring.score_job(job)
        job.score = result
        job.update_status(JobStatus.SCORED)
        if result.recommended_lane:
            job.market_lane = result.recommended_lane
        storage.save_job(job)
        return enrich_job(job)
    except Exception as e:
        raise HTTPException(500, f"Scoring failed: {str(e)}")


# === Resume Generation ===

@app.post("/api/jobs/{job_id}/tailored-resume")
def generate_tailored_resume(job_id: str):
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.score:
        raise HTTPException(400, "Job must be scored first")
    try:
        resume_text = scoring.generate_tailored_resume(job)
        job.tailored_resume = resume_text

        # Generate .docx
        docx_path = docx_builder.generate_resume_docx(
            resume_text, job.id, job.company, job.title
        )
        job.tailored_resume_docx = docx_path
        job.updated_at = datetime.now().isoformat()
        storage.save_job(job)
        return {
            "tailored_resume": resume_text,
            "docx_path": docx_path,
            "docx_filename": Path(docx_path).name,
        }
    except Exception as e:
        raise HTTPException(500, f"Tailored resume generation failed: {str(e)}")


@app.get("/api/jobs/{job_id}/download-resume")
def download_resume(job_id: str):
    """Download the generated .docx resume."""
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.tailored_resume_docx or not Path(job.tailored_resume_docx).exists():
        raise HTTPException(404, "No resume .docx generated yet")
    return FileResponse(
        job.tailored_resume_docx,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=Path(job.tailored_resume_docx).name,
    )


# === Cover Letters ===

@app.post("/api/jobs/{job_id}/cover-letters")
def generate_cover_letters(job_id: str):
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.score:
        raise HTTPException(400, "Job must be scored first")
    try:
        letters = scoring.generate_cover_letters(job)
        # Generate .docx for each variant
        for letter in letters:
            docx_path = docx_builder.generate_cover_letter_docx(
                content=letter.content,
                variant=letter.variant,
                job_id=job.id,
                company=job.company,
                title=job.title,
            )
            letter.docx_path = docx_path
        job.cover_letters = letters
        job.updated_at = datetime.now().isoformat()
        storage.save_job(job)
        return [l.model_dump() for l in letters]
    except Exception as e:
        raise HTTPException(500, f"Cover letter generation failed: {str(e)}")


@app.get("/api/jobs/{job_id}/cover-letters/{letter_id}/download")
def download_cover_letter(job_id: str, letter_id: str):
    """Download a cover letter as .docx."""
    job = storage.load_job(job_id)
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


# === Email ===

@app.post("/api/jobs/{job_id}/send-email")
def send_job_email(job_id: str, data: EmailCompose):
    """Send an email for a job application with optional resume attachment."""
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    config = storage.load_config()

    attachments = []
    if data.attach_resume and job.tailored_resume_docx and Path(job.tailored_resume_docx).exists():
        attachments.append(job.tailored_resume_docx)

    try:
        result = email_service.send_email(
            config=config,
            to=data.to,
            subject=data.subject,
            body=data.body,
            cc=data.cc,
            attachments=attachments,
        )

        # Record the sent email
        job.emails.append(EmailRecord(
            direction="sent",
            to=data.to,
            subject=data.subject,
            body=data.body,
            attachments=[Path(a).name for a in attachments],
        ))
        job.updated_at = datetime.now().isoformat()
        storage.save_job(job)
        return result
    except Exception as e:
        raise HTTPException(500, f"Email send failed: {str(e)}")


@app.post("/api/jobs/{job_id}/parse-confirmation")
def parse_confirmation(job_id: str, raw_email: str = ""):
    """Parse a confirmation/acknowledgment email and extract contact info."""
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not raw_email.strip():
        raise HTTPException(400, "No email text provided")

    try:
        parsed = email_service.parse_confirmation_email(raw_email)

        # Update job with extracted contact info
        if parsed.get("contact_email") and not parsed["contact_email"].startswith("noreply"):
            job.follow_up.contact_email = parsed["contact_email"]
        elif parsed.get("reply_to") and not parsed["reply_to"].startswith("noreply"):
            job.follow_up.contact_email = parsed["reply_to"]
        # If still no contact email, use first non-noreply from regex extraction
        if not job.follow_up.contact_email and parsed.get("contact_emails"):
            job.follow_up.contact_email = parsed["contact_emails"][0]

        if parsed.get("contact_name"):
            job.follow_up.contact_name = parsed["contact_name"]
        if parsed.get("contact_phone"):
            job.follow_up.contact_phone = parsed["contact_phone"]

        # Store all extracted emails and links
        if parsed.get("all_emails"):
            job.follow_up.extracted_emails = parsed["all_emails"]
        if parsed.get("all_urls"):
            job.follow_up.extracted_links = parsed["all_urls"]

        # Build notes from next_steps + portal
        notes_parts = []
        if parsed.get("next_steps"):
            notes_parts.append(parsed["next_steps"])
        if parsed.get("portal_url"):
            notes_parts.append(f"Portal: {parsed['portal_url']}")
        if notes_parts:
            job.follow_up.notes = " | ".join(notes_parts)

        # Record the received email
        job.emails.append(EmailRecord(
            direction="received",
            body=raw_email[:2000],
            subject=f"Confirmation from {job.company}",
        ))

        # Auto-update status if relevant
        if parsed.get("is_interview_request") and job.status == JobStatus.APPLIED:
            job.update_status(JobStatus.INTERVIEW)
        elif parsed.get("is_rejection"):
            job.update_status(JobStatus.REJECTED)

        job.updated_at = datetime.now().isoformat()
        storage.save_job(job)
        return {"parsed": parsed, "job": enrich_job(job)}
    except Exception as e:
        raise HTTPException(500, f"Parsing failed: {str(e)}")


# === Follow-Up Tracking ===

@app.get("/api/follow-ups")
def get_follow_ups():
    jobs = storage.load_all_jobs()
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
def snooze_follow_up(job_id: str, days: int = 7):
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.follow_up.due_at = (datetime.now() + timedelta(days=days)).isoformat()
    job.updated_at = datetime.now().isoformat()
    storage.save_job(job)
    return enrich_job(job)


@app.post("/api/jobs/{job_id}/set-contact")
def set_contact_email(job_id: str, email: str = "", name: str = ""):
    """Set or override the follow-up contact email for a job."""
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if email:
        job.follow_up.contact_email = email
    if name:
        job.follow_up.contact_name = name
    job.updated_at = datetime.now().isoformat()
    storage.save_job(job)
    return enrich_job(job)


@app.post("/api/jobs/{job_id}/mark-followed-up")
def mark_followed_up(job_id: str):
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    config = storage.load_config()
    job.follow_up.sent_at = datetime.now().isoformat()
    job.follow_up.count += 1
    job.follow_up.due_at = (datetime.now() + timedelta(days=config.follow_up_days)).isoformat()
    job.updated_at = datetime.now().isoformat()
    storage.save_job(job)
    return enrich_job(job)


@app.post("/api/follow-ups/send-digest")
def send_follow_up_digest():
    """Send email digest of jobs needing follow-up."""
    config = storage.load_config()
    jobs = storage.load_all_jobs()
    due_jobs = [j for j in jobs if j.follow_up_due]
    try:
        return email_service.send_follow_up_digest(config, due_jobs)
    except Exception as e:
        raise HTTPException(500, f"Email send failed: {str(e)}")


# === Config & Resumes ===

@app.post("/api/test-email")
def test_email():
    """Send a test email to verify SMTP settings."""
    config = storage.load_config()
    if not config.smtp_user or not config.smtp_password:
        raise HTTPException(400, "SMTP not configured. Set Gmail address and App Password first.")
    try:
        email_service.send_email(
            config=config,
            to=config.follow_up_email or config.smtp_user,
            subject="Job Search HQ - Test Email",
            body="This is a test email from your Job Search Command Center.\n\nIf you're reading this, SMTP is working correctly.\n\n— Job Search HQ (http://10.10.10.13:8093)",
        )
        return {"sent": True}
    except Exception as e:
        raise HTTPException(500, f"SMTP test failed: {str(e)}")


@app.get("/api/config")
def get_config():
    return storage.load_config().model_dump()


@app.put("/api/config")
def update_config(config: AppConfig):
    storage.save_config(config)
    return config.model_dump()


@app.get("/api/resumes")
def list_resumes():
    variants = ["director", "base", "contract", "full_history"]
    result = {}
    for v in variants:
        text = storage.load_resume_text(v)
        result[v] = {
            "loaded": bool(text),
            "length": len(text),
            "preview": text[:200] + "..." if len(text) > 200 else text,
        }
    return result


@app.put("/api/resumes/{variant}")
def upload_resume(variant: str, content: str = ""):
    if variant not in ["director", "base", "contract", "full_history"]:
        raise HTTPException(400, "Invalid variant")
    storage.save_resume_text(variant, content)
    return {"ok": True, "variant": variant, "length": len(content)}


# === Stats ===

@app.get("/api/stats")
def dashboard_stats():
    jobs = storage.load_all_jobs()
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


# === Static & Serve ===

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    storage.ensure_dirs()
    docx_builder.ensure_dirs()
    port = int(os.environ.get("APP_PORT", 8093))
    uvicorn.run(app, host="0.0.0.0", port=port)
