"""
JobSpy integration for Job Search Command Center.
Scrapes LinkedIn, Indeed, Glassdoor, Google, ZipRecruiter for job postings.
"""

import logging
from datetime import datetime
from pydantic import BaseModel
from jobspy import scrape_jobs

from models import Job, JobStatus, MarketLane, IntakeSource
import storage
import scoring

logger = logging.getLogger(__name__)


class JobSearchRequest(BaseModel):
    search_term: str
    location: str = "Washington DC-Baltimore Area"
    sites: list[str] = ["indeed", "linkedin", "google"]
    results_wanted: int = 25
    hours_old: int = 72
    is_remote: bool = True
    job_type: str = ""  # fulltime, parttime, contract, internship, or empty for all
    distance: int = 50
    linkedin_fetch_description: bool = True
    country_indeed: str = "USA"
    auto_score: bool = False  # score jobs with full descriptions
    skip_existing: bool = True


class JobSearchResult(BaseModel):
    created: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    total_scraped: int = 0


def find_existing(all_jobs: list[Job], title: str, company: str, url: str) -> Job | None:
    """Check for duplicate by URL or title+company."""
    for j in all_jobs:
        if url and j.url:
            # Normalize URLs for comparison
            u1 = url.rstrip("/").lower()
            u2 = j.url.rstrip("/").lower()
            if u1 == u2:
                return j
        if (title and company and
                title.lower().strip() == j.title.lower().strip() and
                company.lower().strip() == j.company.lower().strip()):
            return j
    return None


def run_search(req: JobSearchRequest) -> JobSearchResult:
    """Execute a JobSpy search and import results into the job tracker."""

    # Build scrape_jobs kwargs
    kwargs = {
        "site_name": req.sites,
        "search_term": req.search_term,
        "location": req.location,
        "results_wanted": req.results_wanted,
        "hours_old": req.hours_old,
        "is_remote": req.is_remote,
        "distance": req.distance,
        "country_indeed": req.country_indeed,
        "linkedin_fetch_description": req.linkedin_fetch_description,
        "description_format": "markdown",
    }

    # Google Jobs needs its own search term with location baked in
    if "google" in req.sites:
        remote_str = " remote" if req.is_remote else ""
        kwargs["google_search_term"] = f"{req.search_term}{remote_str} jobs near {req.location}"

    if req.job_type:
        kwargs["job_type"] = req.job_type

    # Run the scrape
    logger.info(f"JobSpy search: sites={req.sites}, term='{req.search_term}', location='{req.location}'")
    try:
        df = scrape_jobs(**kwargs)
    except Exception as e:
        logger.error(f"JobSpy scrape failed: {e}", exc_info=True)
        return JobSearchResult(errors=[{"error": f"Scrape failed: {str(e)}"}])

    if df is None or df.empty:
        logger.info("JobSpy returned 0 results")
        return JobSearchResult(total_scraped=0)

    # Log per-site breakdown
    if "site" in df.columns:
        site_counts = df["site"].value_counts().to_dict()
        logger.info(f"JobSpy results by site: {site_counts}")
    logger.info(f"JobSpy total results: {len(df)}")

    result = JobSearchResult(total_scraped=len(df))
    all_existing = storage.load_all_jobs() if req.skip_existing else []

    for _, row in df.iterrows():
        try:
            title = str(row.get("title", "")).strip()
            company = str(row.get("company", "")).strip()
            job_url = str(row.get("job_url", "")).strip()
            description = str(row.get("description", "")).strip()
            site = str(row.get("site", "")).strip()

            if not title:
                continue

            # Skip duplicates
            if req.skip_existing:
                existing = find_existing(all_existing, title, company, job_url)
                if existing:
                    result.skipped.append({
                        "id": existing.id,
                        "title": title,
                        "company": company,
                        "reason": "duplicate",
                    })
                    continue

            # Build location string
            city = str(row.get("city", "")).strip() if row.get("city") else ""
            state = str(row.get("state", "")).strip() if row.get("state") else ""
            location_str = ", ".join(filter(None, [city, state]))
            is_remote = bool(row.get("is_remote", False))
            if is_remote:
                location_str = f"{location_str} (Remote)" if location_str else "Remote"

            # Build pay range string
            pay_range = ""
            min_amt = row.get("min_amount")
            max_amt = row.get("max_amount")
            interval = str(row.get("interval", "")).strip() if row.get("interval") else ""
            currency = str(row.get("currency", "USD")).strip() if row.get("currency") else "USD"
            if min_amt and max_amt:
                try:
                    min_val = float(min_amt)
                    max_val = float(max_amt)
                    if interval == "yearly":
                        pay_range = f"${min_val:,.0f} - ${max_val:,.0f}/year"
                    elif interval == "hourly":
                        pay_range = f"${min_val:.0f} - ${max_val:.0f}/hour"
                    else:
                        pay_range = f"${min_val:,.0f} - ${max_val:,.0f}"
                except (ValueError, TypeError):
                    pass

            # Build raw JD
            jd_parts = [description] if description and len(description) > 50 else []
            if not jd_parts:
                jd_parts = [
                    f"{title} at {company}",
                    f"Location: {location_str}" if location_str else "",
                    f"Pay: {pay_range}" if pay_range else "",
                ]
            raw_jd = "\n".join(p for p in jd_parts if p).strip()

            # Determine job type
            job_type_raw = str(row.get("job_type", "")).strip().lower() if row.get("job_type") else ""

            # Build notes
            date_posted = ""
            if row.get("date_posted"):
                try:
                    date_posted = str(row["date_posted"])[:10]
                except Exception:
                    pass

            notes_parts = [f"Source: {site}"]
            if location_str:
                notes_parts.append(f"Location: {location_str}")
            if date_posted:
                notes_parts.append(f"Posted: {date_posted}")
            if job_type_raw:
                notes_parts.append(f"Type: {job_type_raw}")

            # Create the job
            job = Job(
                title=title,
                company=company,
                url=job_url,
                source=f"jobspy_{site}",
                intake_source=IntakeSource.API,
                raw_jd=raw_jd,
                pay_range=pay_range,
                market_lane=MarketLane.CONTRACT if job_type_raw == "contract" else MarketLane.CONTRACT,
                notes=" | ".join(notes_parts),
            )

            storage.save_job(job)
            all_existing.append(job)

            score_val = None
            # Auto-score if requested and we have a real description
            if req.auto_score and len(raw_jd) > 200:
                try:
                    score_result = scoring.score_job(job)
                    job.score = score_result
                    job.update_status(JobStatus.SCORED)
                    if score_result.recommended_lane:
                        job.market_lane = score_result.recommended_lane
                    storage.save_job(job)
                    score_val = score_result.total
                except Exception as e:
                    result.errors.append({
                        "title": title,
                        "company": company,
                        "error": f"scoring failed: {str(e)}",
                    })

            result.created.append({
                "id": job.id,
                "title": title,
                "company": company,
                "location": location_str,
                "pay_range": pay_range,
                "source": site,
                "has_description": len(raw_jd) > 200,
                "score": score_val,
            })

        except Exception as e:
            result.errors.append({
                "title": str(row.get("title", "?")),
                "company": str(row.get("company", "?")),
                "error": str(e),
            })

    return result
