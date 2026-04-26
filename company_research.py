"""
Company research module for Job Search HQ.

Flow:
1. Claude web search → find official domain
2. Claude web search → find leadership/key contacts (primary, works from any IP)
3. Direct site scrape → bonus enrichment if accessible (plain HTML sites only)
4. Build LinkedIn search URLs for each contact
"""

import json
import logging
import re
import time

import ai_router
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Model handled by ai_router
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
SCRAPE_PATHS = [
    "/leadership", "/team", "/our-team", "/about/leadership",
    "/about/team", "/our-people", "/about-us/leadership",
    "/about-us", "/about", "/company",
]
FETCH_TIMEOUT = 8

# ── Claude web search helper ───────────────────────────────────────────────────

def _claude_search(prompt: str, max_tokens: int = 1000) -> str:
    """
    Run a Claude web search. Always uses Anthropic (web_search is Claude-only).
    Returns the last text block — first block is the "I'll search..." preamble.
    """
    return ai_router.web_search_chat(prompt, max_tokens=max_tokens)


# ── Step 1: Find domain ────────────────────────────────────────────────────────

def _find_domain(company: str) -> str | None:
    """Use Claude web search to find the official company domain."""
    logger.info(f"Finding domain for: {company}")
    try:
        text = _claude_search(
            f'Find the official company website domain for "{company}". '
            f'Return ONLY the domain name like "alvarezandmarsal.com" — '
            f'no https://, no www., no trailing slash, nothing else.'
        )
        return _clean_domain(text)
    except Exception as e:
        logger.error(f"Domain search failed: {e}")
        return None


def _clean_domain(text: str) -> str | None:
    text = re.sub(r'^(https?://)?(www\.)?', '', text.strip().lower())
    text = text.split('/')[0].strip()
    if re.match(r'^[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}$', text) and '.' in text:
        return text
    match = re.search(r'([a-z0-9][a-z0-9\-]+\.[a-z]{2,}(?:\.[a-z]{2,})?)', text)
    return match.group(1) if match else None


# ── Step 2: Web search for contacts (primary) ─────────────────────────────────

def _search_contacts(company: str, job_title: str, domain: str | None) -> dict:
    """
    Use Claude web search to find leadership contacts.
    This is the PRIMARY contact discovery method — works regardless of site tech.
    """
    logger.info(f"Web search for contacts: {company}")
    domain_hint = f" (website: {domain})" if domain else ""

    prompt = (
        f'Search for the leadership team and key executives at "{company}"{domain_hint}. '
        f'Find names and titles of people relevant to hiring a {job_title or "Director of Data Analytics"}: '
        f'CEO/President, Chief Data Officer, VP Analytics, VP Data, Head of Data, '
        f'Chief People Officer, VP HR, Head of Talent Acquisition, CHRO, '
        f'and any data/analytics/BI leadership. '
        f'Return ONLY valid JSON with no markdown: '
        f'{{"company_summary": "2-3 sentences about the company", '
        f'"contacts": [{{"name": "Full Name", "title": "Exact Title", '
        f'"notes": "1 sentence why relevant to data analytics director candidate", '
        f'"confidence": "high|medium|low"}}]}}'
    )

    try:
        raw = _claude_search(prompt, max_tokens=2000)
        raw = re.sub(r'^```json\s*|^```\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        logger.error(f"Contact web search failed: {e}")

    return {"company_summary": "", "contacts": []}


# ── Step 3: Direct site scrape (bonus enrichment) ─────────────────────────────

def _try_scrape(domain: str) -> tuple[str, str]:
    """
    Try to scrape the company site for additional contact detail.
    Returns (text, url) or ("", "") if site is inaccessible or JS-rendered.
    """
    for path in SCRAPE_PATHS:
        url = f"https://{domain}{path}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = re.sub(r'\s+', ' ', soup.get_text(separator=" ", strip=True)).strip()
            if len(text) > 500:
                # Score for people content
                lower = text.lower()
                score = sum(lower.count(kw) for kw in [
                    "ceo", "cto", "cfo", "president", "vice president", "vp ",
                    "director", "founder", "chief", "officer"
                ])
                if score >= 3:
                    logger.info(f"Scraped {url} (score={score})")
                    return text[:5000], url
        except Exception as e:
            logger.debug(f"Scrape failed {url}: {e}")
        time.sleep(0.2)

    return "", ""


def _enrich_from_scrape(contacts: list[dict], page_text: str) -> list[dict]:
    """
    Try to add email addresses found in scraped text to existing contacts.
    """
    emails_found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', page_text)
    # Only keep non-generic emails
    emails_found = [e for e in emails_found if not any(
        g in e.lower() for g in ['noreply', 'info@', 'support@', 'contact@', 'hello@']
    )]
    # If exactly one email per contact name pattern, try to match
    # For now just attach first found email to first contact if only one contact
    if len(contacts) == 1 and len(emails_found) == 1:
        contacts[0]['email'] = emails_found[0]
    return contacts


# ── Main entry point ───────────────────────────────────────────────────────────

def research_company(company: str, job_title: str = "", user_id: str = "") -> dict:
    """
    Research a company to find key contacts.
    Primary: Claude web search. Bonus: direct site scrape for enrichment.
    """
    logger.info(f"=== Researching: {company} ===")
    result = {
        "contacts": [],
        "company_summary": "",
        "source_url": "",
        "searches_run": [],
    }

    # Step 1: Find domain
    domain = _find_domain(company)
    result["searches_run"].append(
        f"Domain search: {'found ' + domain if domain else 'not found'}"
    )
    logger.info(f"Domain: {domain}")

    # Step 2: Web search for contacts (primary — always runs)
    extracted = _search_contacts(company, job_title, domain)
    result["company_summary"] = extracted.get("company_summary", "")
    raw_contacts = extracted.get("contacts", [])
    result["searches_run"].append(f"Leadership web search: {len(raw_contacts)} contacts found")
    logger.info(f"Web search found {len(raw_contacts)} contacts")

    # Step 3: Bonus — try direct scrape for enrichment
    if domain:
        page_text, source_url = _try_scrape(domain)
        if source_url:
            result["source_url"] = source_url
            result["searches_run"].append(f"Site scrape: {source_url}")
            if raw_contacts and page_text:
                raw_contacts = _enrich_from_scrape(raw_contacts, page_text)
        else:
            result["searches_run"].append("Site scrape: blocked or JS-rendered")

    # Step 4: Build output with LinkedIn search URLs
    contacts = []
    for c in raw_contacts[:6]:
        name = c.get("name", "").strip()
        if not name:
            continue
        li_query = requests.utils.quote(f"{name} {company}")
        contacts.append({
            "name": name,
            "title": c.get("title", ""),
            "email": c.get("email", ""),
            "linkedin_url": "",
            "linkedin_search_url": (
                f"https://www.linkedin.com/search/results/people/?keywords={li_query}"
            ),
            "notes": c.get("notes", ""),
            "confidence": c.get("confidence", "medium"),
        })

    result["contacts"] = contacts
    logger.info(
        f"=== Done: {company} | {len(contacts)} contacts | "
        f"source: {result['source_url'] or 'web search only'} ==="
    )
    return result
