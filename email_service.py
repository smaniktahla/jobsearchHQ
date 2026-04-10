"""
Email service for Job Search Command Center.
- Send emails with .docx attachments via Gmail SMTP
- Parse confirmation emails to extract contact info
- IMAP inbox search (future)
"""

import smtplib
import re
import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from models import AppConfig, Job
import anthropic


def send_email(
    config: AppConfig,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    attachments: list[str] = None,
) -> dict:
    """Send an email via Gmail SMTP with optional file attachments."""
    if not config.smtp_user or not config.smtp_password:
        raise ValueError("SMTP not configured. Set Gmail address and App Password in Settings.")

    msg = MIMEMultipart()
    msg["From"] = config.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc

    msg.attach(MIMEText(body, "plain"))

    # Attach files
    if attachments:
        for filepath in attachments:
            path = Path(filepath)
            if not path.exists():
                continue
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={path.name}",
            )
            msg.attach(part)

    recipients = [to]
    if cc:
        recipients.extend([addr.strip() for addr in cc.split(",") if addr.strip()])

    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.smtp_user, recipients, msg.as_string())

    return {"sent": True, "to": to, "subject": subject}


def send_follow_up_digest(config: AppConfig, due_jobs: list[Job]) -> dict:
    """Send a follow-up digest email."""
    if not due_jobs:
        return {"sent": False, "reason": "No follow-ups due"}

    lines = [f"You have {len(due_jobs)} job(s) due for follow-up:\n"]
    for j in due_jobs:
        days = j.days_since_applied or 0
        count = j.follow_up.count
        lines.append(f"\u2022 {j.company} \u2014 {j.title}")
        lines.append(f"  Applied {days} days ago | Follow-ups sent: {count}")
        if j.follow_up.contact_email:
            lines.append(f"  Contact: {j.follow_up.contact_email}")
        if j.url:
            lines.append(f"  {j.url}")
        lines.append("")

    lines.append("---")
    lines.append("Manage at http://10.10.10.13:8093")

    return send_email(
        config=config,
        to=config.follow_up_email,
        subject=f"Job Search: {len(due_jobs)} follow-up(s) due",
        body="\n".join(lines),
    )


def parse_confirmation_email(raw_email: str) -> dict:
    """
    Extract contact info from a confirmation/acknowledgment email.
    Uses regex for emails/URLs, then Claude for semantic analysis.
    Returns structured data with all extracted artifacts.
    """
    # Step 1: Regex extraction of all emails and URLs
    email_pattern = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
    url_pattern = r'https?://[^\s<>"\')\]]+|(?<!\S)[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}(?:/[^\s<>"\')\]]*)?'

    raw_emails = list(set(re.findall(email_pattern, raw_email)))
    raw_urls = list(set(re.findall(url_pattern, raw_email)))

    # Filter out common noreply patterns for the "best" contact
    noreply_patterns = ['noreply', 'no-reply', 'donotreply', 'do-not-reply', 'mailer-daemon', 'postmaster']
    contact_emails = [e for e in raw_emails if not any(p in e.lower() for p in noreply_patterns)]
    noreply_emails = [e for e in raw_emails if any(p in e.lower() for p in noreply_patterns)]

    # Step 2: Claude analysis for semantic understanding
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[
            {"role": "user", "content": f"""Extract contact and follow-up information from this email. Return ONLY valid JSON, no markdown:
{{
  "company": "company name",
  "role_title": "job title if mentioned",
  "contact_email": "best email for follow-up (NOT noreply). Empty string if none found.",
  "contact_name": "recruiter or contact person name if available",
  "contact_phone": "phone number if available",
  "reply_to": "the best email to use for follow-up, empty if none",
  "is_confirmation": true/false,
  "is_rejection": true/false,
  "is_interview_request": true/false,
  "next_steps": "any mentioned next steps, timeline, or instructions",
  "portal_url": "URL to check application status if mentioned",
  "notes": "any other useful info for follow-up"
}}

Extracted emails found in text: {json.dumps(raw_emails)}
Extracted URLs found in text: {json.dumps(raw_urls)}

Email:
{raw_email[:4000]}"""}
        ],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError:
        parsed = {"error": "Failed to parse", "raw": raw[:500]}

    # Merge regex results into the response
    parsed["all_emails"] = raw_emails
    parsed["contact_emails"] = contact_emails
    parsed["noreply_emails"] = noreply_emails
    parsed["all_urls"] = raw_urls

    return parsed
