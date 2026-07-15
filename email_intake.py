"""
Gmail LinkedIn Job Alert intake.
Polls a Gmail label via IMAP and creates Job entries from LinkedIn's
job alert digest emails, the same way jobspy_search/linkedin_intake do.
"""

import email
import imaplib
import re
from bs4 import BeautifulSoup

from models import Job, IntakeSource, MarketLane, ReviewStatus
import storage

GMAIL_LABEL = "LinkedIn Job Alerts"
GMAIL_IMAP_HOST = "imap.gmail.com"

# DC-metro location tokens — mirrors the "on-site only outside DC metro" deal breaker.
DC_METRO_TOKENS = (
    "remote", "dc", "washington", "virginia", " va", "va)", "maryland", " md", "md)",
    "arlington", "alexandria", "tysons", "reston", "bethesda", "rockville", "mclean", "vienna",
)

# Titles below Manager level, for W2 roles only — contract/1099 has no seniority floor.
JUNIOR_TITLE_TOKENS = (
    "junior", "jr.", "entry level", "entry-level", "intern", "internship",
    "associate", "coordinator", "specialist", "analyst i", "analyst ii", "analyst 1", "analyst 2",
)
SENIOR_ENOUGH_TOKENS = (
    "manager", "director", "vp", "vice president", "head of", "principal", "lead", "chief",
)

# Pre-filter pay floors for thin digest listings — set explicitly higher than
# config.w2_salary_min/contract_hourly_min since this is a stricter first-pass
# cut, not the full scoring engine's target range.
PREFILTER_W2_FLOOR = 180000
PREFILTER_HOURLY_FLOOR = 60


def _prefilter_reason(title: str, company: str, location: str, pay_range: str, deal_breakers: list[str],
                       w2_floor: float, hourly_floor: float) -> str | None:
    """
    Cheap, deterministic rejection pass for thin (title/company/location/pay-only)
    listings — these lack a full JD so the real LLM scorer (scoring.score_job)
    can't run yet. Returns a rejection reason string, or None if the listing
    should proceed to normal triage (Refresh From URL + scoring).
    """
    title_l = title.lower()
    location_l = location.lower()
    is_hourly = bool(re.search(r"/\s*hour", pay_range, re.I))

    # Deal breakers (relocate / TS-SCI / etc.) — check title + location text
    combined = f"{title_l} {location_l}"
    for breaker in deal_breakers:
        if breaker.lower() in combined:
            return f"Deal breaker: {breaker}"

    # Location — only reject if a location was actually disclosed
    if location_l and not any(tok in location_l for tok in DC_METRO_TOKENS):
        return f"Outside DC metro / not remote: {location}"

    # Pay floor
    if pay_range:
        amounts = [float(a.replace(",", "")) * (1000 if "k" in pay_range.lower() else 1)
                   for a in re.findall(r"[\d,]+(?:\.\d+)?", pay_range)]
        if amounts:
            max_amt = max(amounts)
            if is_hourly and max_amt < hourly_floor:
                return f"Below ${hourly_floor:.0f}/hr floor: {pay_range}"
            if not is_hourly and max_amt < w2_floor:
                return f"Below ${w2_floor:,.0f}/yr floor: {pay_range}"

    # Seniority floor — W2 roles only (contract/hourly listings have no floor)
    if not is_hourly:
        has_junior_token = any(tok in title_l for tok in JUNIOR_TITLE_TOKENS)
        has_senior_token = any(tok in title_l for tok in SENIOR_ENOUGH_TOKENS)
        if has_junior_token and not has_senior_token:
            return f"Below Manager level: {title}"

    return None


def _extract_jobs_from_html(html: str) -> list[dict]:
    """
    LinkedIn's digest email links each job's title/thumbnail/summary via three
    separate <a> tags sharing the same jobs/view/<id> href. The summary anchor's
    text is "{title} {company} · {location} [pay] [Actively recruiting|Apply]"
    and the title-only anchor's text is a strict prefix of it, so title = the
    shortest non-empty anchor text and the rest is derived from the longest.
    """
    soup = BeautifulSoup(html, "html.parser")
    by_id: dict[str, set[str]] = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"jobs/view/(\d+)", a["href"])
        if not m:
            continue
        text = a.get_text(" ", strip=True)
        if text:
            by_id.setdefault(m.group(1), set()).add(text)

    jobs = []
    for jid, texts in by_id.items():
        texts = sorted(texts, key=len)
        title = texts[0]
        combined = texts[-1]
        remainder = combined[len(title):].strip() if combined.startswith(title) else ""
        company, _, rest = remainder.partition(" · ")

        pay_match = re.search(r"\$[\d,]+K?\s*-\s*\$[\d,]+K?\s*/\s*\w+", rest)
        pay_range = pay_match.group(0) if pay_match else ""
        location = rest
        for marker in (pay_range, "Actively recruiting", "Easy Apply", "Apply"):
            if marker:
                location = location.replace(marker, "")
        location = location.strip(" ·").strip()

        jobs.append({
            "linkedin_job_id": jid,
            "title": title,
            "company": company.strip(),
            "location": location,
            "pay_range": pay_range,
            "url": f"https://www.linkedin.com/jobs/view/{jid}/",
        })
    return jobs


def fetch_alert_emails(smtp_user: str, smtp_password: str) -> list[dict]:
    """Connect via IMAP and pull HTML bodies of unread emails in the alerts label."""
    imap = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST)
    imap.login(smtp_user, smtp_password)
    imap.select(f'"{GMAIL_LABEL}"')
    status, data = imap.search(None, "UNSEEN")
    ids = data[0].split()

    emails = []
    for msg_id in ids:
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        html_body = None
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="ignore"
                )
                break
        if html_body:
            emails.append({"subject": msg["subject"], "date": msg["date"], "html": html_body})

    imap.logout()
    return emails


def find_existing_job(all_jobs: list[Job], url: str, title: str, company: str) -> Job | None:
    for j in all_jobs:
        if url and j.url and url.rstrip("/") == j.url.rstrip("/"):
            return j
        if title.lower() == j.title.lower() and company.lower() == j.company.lower():
            return j
    return None


def process_linkedin_alerts(user_id: str) -> dict:
    """Fetch unread LinkedIn Job Alert emails and create Job entries for new listings."""
    config = storage.load_config(user_id)
    if not config.smtp_user or not config.smtp_password:
        return {"error": "No Gmail credentials configured (smtp_user/smtp_password)"}

    raw_emails = fetch_alert_emails(config.smtp_user, config.smtp_password)
    all_existing = storage.load_all_jobs(user_id)
    created, skipped, errors = [], [], []

    for raw in raw_emails:
        try:
            parsed_jobs = _extract_jobs_from_html(raw["html"])
        except Exception as e:
            errors.append({"email_subject": raw.get("subject", ""), "error": f"parse failed: {e}"})
            continue

        for pj in parsed_jobs:
            title, company, url = pj["title"], pj["company"], pj["url"]
            if not title or not company:
                continue

            existing = find_existing_job(all_existing, url, title, company)
            if existing:
                skipped.append({"id": existing.id, "title": title, "company": company, "reason": "duplicate"})
                continue

            raw_jd = "\n".join(part for part in [
                f"{title} at {company}",
                f"Location: {pj['location']}" if pj["location"] else "",
                f"Pay: {pj['pay_range']}" if pj["pay_range"] else "",
            ] if part).strip()

            try:
                is_hourly = bool(re.search(r"/\s*hour", pj["pay_range"], re.I))
                job = Job(
                    title=title,
                    company=company,
                    url=url,
                    source="linkedin_email_alert",
                    intake_source=IntakeSource.API,
                    raw_jd=raw_jd,
                    pay_range=pj["pay_range"],
                    market_lane=MarketLane.CONTRACT if is_hourly else MarketLane.W2_SNIPER,
                    notes=f"LinkedIn Job Alert email: {raw.get('subject', '')}",
                )

                reason = _prefilter_reason(
                    title, company, pj["location"], pj["pay_range"], config.deal_breakers,
                    w2_floor=PREFILTER_W2_FLOOR, hourly_floor=PREFILTER_HOURLY_FLOOR,
                )
                if reason:
                    job.review_status = ReviewStatus.REJECTED
                    job.notes += f" | Auto-rejected: {reason}"

                storage.save_job(user_id, job)
                all_existing.append(job)
                created.append({"id": job.id, "title": title, "company": company, "rejected": bool(reason)})
            except Exception as e:
                errors.append({"title": title, "company": company, "error": str(e)})

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "emails_processed": len(raw_emails),
        "count_created": len(created),
        "count_skipped": len(skipped),
        "count_errors": len(errors),
    }
