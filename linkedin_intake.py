"""
LinkedIn intake module.
Receives raw page text from the browser extension, uses Claude to parse
job listings, and creates Job entries.
"""

import json
import os
import anthropic
from pydantic import BaseModel
from models import Job, JobStatus, MarketLane, IntakeSource
import storage
import scoring


class LinkedInJobLink(BaseModel):
    linkedin_job_id: str = ""
    job_url: str = ""
    link_text: str = ""


class LinkedInCapture(BaseModel):
    search_query: str = ""
    search_url: str = ""
    current_job_id: str = ""
    selected_job_description: str = ""
    job_list_text: str = ""
    job_links: list[LinkedInJobLink] = []
    auto_score: bool = True
    captured_at: str = ""


PARSE_PROMPT = """You are parsing LinkedIn job search results from raw page text.
Extract every distinct job listing you can find. For each job, extract:
- title: the job title
- company: the company name
- location: location if visible
- posted: when it was posted (e.g. "2 days ago") if visible
- easy_apply: true if "Easy Apply" appears near this listing

The text contains a list of job cards, possibly with some navigation/UI noise mixed in.
Each job card typically has: Title, Company, Location, Posted time.

Return ONLY a valid JSON array. No markdown, no commentary. Example:
[
  {{"title": "Senior Data Engineer", "company": "Acme Corp", "location": "Remote", "posted": "3 days ago", "easy_apply": true}},
  {{"title": "Director of Analytics", "company": "BigCo", "location": "Washington, DC", "posted": "1 week ago", "easy_apply": false}}
]

If you cannot find any jobs, return an empty array: []

Raw page text:
{text}"""


def parse_job_list_text(text: str) -> list[dict]:
    """Use Claude to extract structured job listings from raw LinkedIn page text."""
    if not text or len(text.strip()) < 50:
        return []

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    # Truncate to avoid token limits — 25K chars is plenty for a search results page
    truncated = text[:25000]

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[
            {"role": "user", "content": PARSE_PROMPT.format(text=truncated)}
        ],
    )

    raw = message.content[0].text.strip()
    # Clean markdown fencing
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        jobs = json.loads(raw)
        if isinstance(jobs, list):
            return jobs
    except json.JSONDecodeError:
        pass

    return []


def parse_selected_job_description(description: str) -> dict:
    """Use Claude to extract structured info from a full job description."""
    if not description or len(description.strip()) < 100:
        return {}

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[
            {"role": "user", "content": f"""Extract from this LinkedIn job description. Return ONLY valid JSON:
{{
  "title": "job title",
  "company": "company name",
  "location": "location",
  "pay_range": "salary/rate if mentioned, empty string if not",
  "is_contract": false,
  "is_remote": false,
  "easy_apply": false
}}

Job description:
{description[:5000]}"""}
        ],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {}


def find_existing_job(all_jobs: list[Job], title: str, company: str, url: str) -> Job | None:
    """Check if a job already exists by LinkedIn URL or title+company match."""
    for j in all_jobs:
        # Match by URL (LinkedIn job ID)
        if url and j.url and url.rstrip("/") == j.url.rstrip("/"):
            return j
        # Match by title + company (fuzzy)
        if (title.lower() == j.title.lower() and
                company.lower() == j.company.lower()):
            return j
    return None


def process_linkedin_capture(capture: LinkedInCapture) -> dict:
    """
    Process a LinkedIn page capture:
    1. Parse job list text with Claude
    2. Match to extracted links for URLs
    3. Handle the selected/expanded job specially (has full JD)
    4. Create jobs, optionally auto-score
    """
    all_existing = storage.load_all_jobs()
    created = []
    skipped = []
    errors = []

    # Build a lookup from link_text/job_id to URL
    link_lookup = {}
    for link in capture.job_links:
        if link.linkedin_job_id:
            link_lookup[link.linkedin_job_id] = link.job_url
        if link.link_text:
            link_lookup[link.link_text.lower().strip()] = link.job_url

    # --- Handle the selected/expanded job (has full description) ---
    if capture.selected_job_description and len(capture.selected_job_description) > 200:
        meta = parse_selected_job_description(capture.selected_job_description)

        title = meta.get("title", "")
        company = meta.get("company", "")
        job_url = ""

        # Try to match URL from links
        if capture.current_job_id:
            job_url = f"https://www.linkedin.com/jobs/view/{capture.current_job_id}/"
        elif capture.current_job_id in link_lookup:
            job_url = link_lookup[capture.current_job_id]

        existing = find_existing_job(all_existing, title, company, job_url)
        if existing:
            skipped.append({"id": existing.id, "title": title, "company": company, "reason": "duplicate"})
        elif title:
            try:
                job = Job(
                    title=title,
                    company=company,
                    url=job_url,
                    source="linkedin_extension",
                    intake_source=IntakeSource.API,
                    raw_jd=capture.selected_job_description,
                    pay_range=meta.get("pay_range", ""),
                    market_lane=MarketLane.CONTRACT if meta.get("is_contract") else MarketLane.CONTRACT,
                    notes=f"LinkedIn: {capture.search_query} | {capture.search_url}",
                )
                storage.save_job(job)
                all_existing.append(job)

                score_val = None
                if capture.auto_score:
                    try:
                        result = scoring.score_job(job)
                        job.score = result
                        job.update_status(JobStatus.SCORED)
                        if result.recommended_lane:
                            job.market_lane = result.recommended_lane
                        storage.save_job(job)
                        score_val = result.total
                    except Exception as e:
                        errors.append({"title": title, "company": company, "error": f"score: {e}"})

                created.append({"id": job.id, "title": title, "company": company, "score": score_val})
            except Exception as e:
                errors.append({"title": title, "company": company, "error": str(e)})

    # --- Parse the job list text ---
    if capture.job_list_text and len(capture.job_list_text) > 100:
        parsed_jobs = parse_job_list_text(capture.job_list_text)

        for pj in parsed_jobs:
            title = pj.get("title", "").strip()
            company = pj.get("company", "").strip()
            if not title:
                continue

            # Try to find URL from links
            job_url = ""
            title_lower = title.lower().strip()
            for link in capture.job_links:
                # Match by link text containing the title
                if title_lower in link.link_text.lower():
                    job_url = link.job_url
                    break
            if not job_url:
                # Check link_lookup
                job_url = link_lookup.get(title_lower, "")

            existing = find_existing_job(all_existing, title, company, job_url)
            if existing:
                skipped.append({"id": existing.id, "title": title, "company": company, "reason": "duplicate"})
                continue

            # Build a minimal JD from parsed info
            raw_jd = "\n".join(part for part in [
                f"{title} at {company}",
                f"Location: {pj.get('location', '')}" if pj.get("location") else "",
                f"Posted: {pj.get('posted', '')}" if pj.get("posted") else "",
                "Easy Apply" if pj.get("easy_apply") else "",
            ] if part).strip()

            try:
                job = Job(
                    title=title,
                    company=company,
                    url=job_url,
                    source="linkedin_extension",
                    intake_source=IntakeSource.API,
                    raw_jd=raw_jd,
                    notes=f"LinkedIn: {capture.search_query}",
                )
                storage.save_job(job)
                all_existing.append(job)

                score_val = None
                if capture.auto_score and len(raw_jd) > 200:
                    # Only auto-score if we have meaningful JD text
                    try:
                        result = scoring.score_job(job)
                        job.score = result
                        job.update_status(JobStatus.SCORED)
                        if result.recommended_lane:
                            job.market_lane = result.recommended_lane
                        storage.save_job(job)
                        score_val = result.total
                    except Exception as e:
                        errors.append({"title": title, "company": company, "error": f"score: {e}"})

                created.append({"id": job.id, "title": title, "company": company, "score": score_val})
            except Exception as e:
                errors.append({"title": title, "company": company, "error": str(e)})

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "count_created": len(created),
        "count_skipped": len(skipped),
        "count_errors": len(errors),
    }
