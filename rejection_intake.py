"""Mark applied jobs rejected when the rejection email lands in the Gmail inbox.

Reads the inbox over IMAP (read-only, never changes flags), keeps only mail that both sounds like an
application outcome and names a company we applied to, then matches it to one applied/interview job by
title. A confident match flips the job to REJECTED and logs the email on it. Anything else changes
nothing: unmatched rejections and interview/offer-sounding mail are only reported in the summary.

Deterministic on purpose: a wrong "rejected" hides a live application, so there is no model in the loop.
Only jobs already in applied/interview status are ever touched.
"""
import email
import imaplib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from html import unescape

import storage
from models import AgentEvent, AgentEventType, JobStatus

logger = logging.getLogger(__name__)

GMAIL_IMAP_HOST = "imap.gmail.com"
DEFAULT_LOOKBACK_DAYS = 14
MAX_MESSAGES = 500
BODY_CHARS = 8000
SEEN_KEEP = 5000

REJECTION_PHRASES = (
    "not moving forward", "will not be moving forward", "not be moving forward", "not selected",
    "not been selected", "unfortunately", "no longer available", "no longer under consideration",
    "other candidates", "decided to pursue", "decided to move forward with", "position has been filled",
    "role has been filled", "just hired someone", "we regret to inform", "will not be proceeding",
    "not able to offer you", "chosen to move forward with another", "has been closed",
)
INTEREST_PHRASES = (
    "schedule an interview", "like to schedule", "invite you to interview", "invite you to an interview",
    "phone screen", "next steps", "would like to speak with you", "like to set up a call",
    "move forward with your application", "extend an offer", "pleased to offer",
)
APPLICATION_SUBJECT = re.compile(r"application|interest in|applying|candidacy|status|position|role|opportunity", re.I)
_COMPANY_NOISE = {"inc", "inc.", "llc", "ltd", "corp", "corporation", "co", "company", "usa", "us", "the", "group", "holdings"}


def _seen_path(user_id: str):
    return storage.get_user_dir(user_id) / "rejection_seen.json"


def _load_seen(user_id: str) -> list[str]:
    try:
        return json.loads(_seen_path(user_id).read_text())
    except (OSError, ValueError):
        return []


def _save_seen(user_id: str, seen: list[str]) -> None:
    p = _seen_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(seen[-SEEN_KEEP:]))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unescape(s or "").lower()).strip()


def _company_core(company: str) -> str:
    """'BAE Systems USA' -> 'bae systems'; the part of the name an email is likely to repeat."""
    words = [w for w in _norm(company).split() if w not in _COMPANY_NOISE]
    return " ".join(words)


def _body_text(msg) -> str:
    plain = html = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = raw
        elif ctype == "text/html" and not html:
            html = raw
    if not plain and html:
        plain = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", html))
    return re.sub(r"\s+", " ", unescape(plain))[:BODY_CHARS]


def _header(msg, name: str) -> str:
    return str(make_header(decode_header(msg.get(name, ""))))


def _gmail_query(days: int) -> str:
    """Gmail-side search (X-GM-RAW) so the ~150 messages/day of newsletters are never downloaded."""
    phrases = " OR ".join(f'\\"{p}\\"' for p in REJECTION_PHRASES + INTEREST_PHRASES)
    return f"in:inbox newer_than:{days}d ({phrases})"


def fetch_inbox(smtp_user: str, smtp_password: str, days: int, skip_ids: set | None = None) -> list[dict]:
    """Inbox messages from the last `days` days that Gmail says contain outcome-sounding phrases.

    Read-only: message flags are never touched. Messages whose Message-ID is in skip_ids are not downloaded."""
    imap = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST)
    try:
        imap.login(smtp_user, smtp_password)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "X-GM-RAW", f'"{_gmail_query(days)}"')
        ids = data[0].split()[-MAX_MESSAGES:]
        out = []
        for i in ids:
            _, md = imap.fetch(i, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            mid = ""
            if md and md[0]:
                mid = (email.message_from_bytes(md[0][1]).get("Message-ID") or "").strip()
            if mid and mid in (skip_ids or ()):
                continue
            _, full = imap.fetch(i, "(BODY.PEEK[])")
            if not full or not full[0]:
                continue
            msg = email.message_from_bytes(full[0][1])
            out.append({"id": mid, "from": _header(msg, "From"), "subject": _header(msg, "Subject"),
                        "date": msg.get("Date", ""), "body": _body_text(msg)})
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def classify(subject: str, body: str) -> str:
    """'rejection' | 'interest' | ''. Rejection wins: 'not moving forward' also contains 'moving forward'."""
    text = f"{subject} {body}".lower()
    if any(p in text for p in REJECTION_PHRASES):
        return "rejection"
    if any(p in text for p in INTEREST_PHRASES):
        return "interest"
    return ""


def match_job(jobs: list, subject: str, sender: str, body: str):
    """The one applied/interview job this email is about, or None.

    Needs the company name in the email AND the job's whole title in the subject/body. When several
    titles match (one title can be a prefix of another) the longest wins; a tie means no match.
    """
    haystack = _norm(f"{sender} {subject} {body}")
    best, tied = None, False
    for job in jobs:
        core = _company_core(job.company)
        if not core or not re.search(rf"\b{re.escape(core)}\b", haystack):
            continue
        title = _norm(job.title)
        if not title or title not in haystack:
            continue
        if best is None or len(title) > len(_norm(best.title)):
            best, tied = job, False
        elif len(title) == len(_norm(best.title)) and job.id != best.id:
            tied = True
    return None if tied else best


def run(user_id: str, config=None, lookback_days: int = DEFAULT_LOOKBACK_DAYS, dry_run: bool = False,
        notify: bool = True) -> dict:
    """Scan the inbox. dry_run reports what would change without writing anything or remembering messages."""
    config = config or storage.load_config(user_id)
    if not config.smtp_user or not config.smtp_password:
        return {"error": "No Gmail credentials configured (smtp_user/smtp_password)"}
    if config.job_search_paused:
        return {"error": "Job search is paused"}

    jobs = [j for j in storage.load_all_jobs(user_id, copy=False)
            if j.status in (JobStatus.APPLIED, JobStatus.INTERVIEW)]
    seen = _load_seen(user_id)
    seen_set = set(seen)
    messages = fetch_inbox(config.smtp_user, config.smtp_password, lookback_days, seen_set)

    rejected, interest, unmatched, scanned = [], [], [], 0
    for m in messages:
        if m["id"] and m["id"] in seen_set:
            continue
        scanned += 1
        if m["id"]:
            seen.append(m["id"])
        kind = classify(m["subject"], m["body"])
        if not kind:
            continue
        job = match_job(jobs, m["subject"], m["from"], m["body"])
        row = {"subject": m["subject"], "from": m["from"], "date": m["date"]}
        if job is None:
            if kind == "rejection" and APPLICATION_SUBJECT.search(m["subject"]):
                unmatched.append(row)
            continue
        row.update(id=job.id, company=job.company, title=job.title)
        if any(r["id"] == job.id for r in rejected):   # same rejection emailed twice
            continue
        if kind == "interest":
            interest.append(row)
            continue
        if job.status != JobStatus.APPLIED:   # an interview in progress is flagged, never auto-rejected
            interest.append({**row, "note": f"rejection-sounding email, but the job is in {job.status.value}"})
            continue
        rejected.append(row)
        if not dry_run:
            live = storage.load_job(user_id, job.id)   # cache objects are read-only
            if live and live.status == JobStatus.APPLIED:
                live.update_status(JobStatus.REJECTED)
                live.agent_log.append(AgentEvent(
                    event_type=AgentEventType.RESOLVE, success=True,
                    notes=f'rejected per inbox email "{m["subject"]}" ({m["date"]})'))
                live.updated_at = datetime.now(timezone.utc).isoformat()
                storage.save_job(user_id, live)

    if not dry_run:
        _save_seen(user_id, seen)
        if notify and (rejected or interest or unmatched):
            _notify(config, rejected, interest, unmatched)
    logger.info(f"[{user_id}] Inbox scan: {scanned} new messages, {len(rejected)} rejected, "
                f"{len(interest)} flagged, {len(unmatched)} unmatched")
    return {"scanned": scanned, "rejected": rejected, "interest": interest, "unmatched": unmatched,
            "dry_run": dry_run}


def _notify(config, rejected: list, interest: list, unmatched: list) -> None:
    if not (config.smtp_user and config.smtp_password and config.author_email):
        return
    try:
        import email_service
        lines = []
        if rejected:
            lines += [f"Marked {len(rejected)} job(s) rejected from your inbox:", ""]
            lines += [f"  {r['company']} - {r['title']}" for r in rejected] + [""]
        if interest:
            lines += ["Mail that looks like progress (no change made, please check):", ""]
            lines += [f"  {r['company']} - {r['title']}: {r['subject']}" for r in interest] + [""]
        if unmatched:
            lines += ["Rejection-like emails I could not match to an applied job (no change made):", ""]
            lines += [f"  {r['from']}: {r['subject']}" for r in unmatched[:20]] + [""]
        lines.append("Wrong match? Set the job back to Applied in JSHQ.")
        email_service.send_email(config, config.author_email, "JSHQ inbox scan", "\n".join(lines))
    except Exception as e:
        logger.warning(f"Inbox scan summary email failed: {e}")
