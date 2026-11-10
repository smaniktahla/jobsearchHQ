"""
Company research module for Job Search HQ.

Flow:
1. Claude web search → find official domain
2. Claude web search → find leadership/key contacts (primary, works from any IP)
3. Direct site scrape → bonus enrichment if accessible (plain HTML sites only)
4. Google email search → per-contact Claude web search for actual email address (parallel)
5. Build LinkedIn search URLs + Google email search URLs for each contact
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

class ResearchError(RuntimeError):
    """The search backend failed (not "searched and found nothing"). Callers surface this to the user."""


def _api_error_message(e: Exception) -> str:
    """One-line reason from an Anthropic/HTTP error, e.g. 'credit balance too low'."""
    detail = getattr(e, "message", None)
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        detail = (body.get("error") or {}).get("message") or detail
    detail = str(detail or e)
    if "credit balance is too low" in detail:
        return "Anthropic API credit balance too low"
    return detail[:200]


def _find_domain(company: str) -> str | None:
    """Use Claude web search to find the official company domain.

    Returns None only when the search ran and gave no usable domain; a backend failure raises ResearchError.
    """
    logger.info(f"Finding domain for: {company}")
    try:
        text = _claude_search(
            f'Find the official company website domain for "{company}". '
            f'Return ONLY the domain name like "alvarezandmarsal.com" — '
            f'no https://, no www., no trailing slash, nothing else.'
        )
    except Exception as e:
        logger.error(f"Domain search failed: {e}")
        raise ResearchError(_api_error_message(e)) from e
    return _clean_domain(text)


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
    except Exception as e:
        logger.error(f"Contact web search failed: {e}")
        raise ResearchError(_api_error_message(e)) from e
    try:
        raw = re.sub(r'^```json\s*|^```\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        logger.error(f"Contact search returned unparseable JSON: {e}")

    return {"company_summary": "", "contacts": []}


# ── Local fallback: SearXNG + local LLM ───────────────────────────────────────
# Used only when Claude web search raises ResearchError (e.g. API credit exhausted).

SEARXNG_URL = os.environ.get("RESEARCH_SEARXNG_URL", "http://10.10.10.244:8081")
FALLBACK_LLM_URL = os.environ.get("RESEARCH_FALLBACK_LLM_URL", "http://10.10.10.105:8081")  # AI1 llama-server
FALLBACK_LLM_MODEL = os.environ.get("RESEARCH_FALLBACK_LLM_MODEL", "qwen35-4b")  # alias; model is Gemma4 26B-A4B


class _FallbackLLMConfig:
    ollama_url = FALLBACK_LLM_URL
    ollama_model = FALLBACK_LLM_MODEL


def _searx(query: str, limit: int = 8) -> list[dict]:
    """Query SearXNG. Raises ResearchError if it is unreachable or returns nothing."""
    try:
        r = requests.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        raise ResearchError(f"SearXNG search failed: {str(e)[:150]}") from e
    out = [
        {"title": x.get("title", ""), "url": x.get("url", ""), "content": x.get("content", "")}
        for x in results if x.get("url")
    ][:limit]
    if not out:
        raise ResearchError(f"SearXNG returned no results for: {query}")
    return out


def _fallback_llm_json(system: str, user_msg: str, max_tokens: int = 1500) -> dict:
    try:
        raw = ai_router._ollama_chat(
            system, user_msg, _FallbackLLMConfig, json_mode=True, max_tokens=max_tokens, timeout=180
        )
        return json.loads(raw)
    except Exception as e:
        raise ResearchError(f"local LLM extraction failed: {str(e)[:150]}") from e


def _host(url: str) -> str:
    m = re.match(r'^(?:https?://)?([^/:?#]+)', url.strip().lower())
    return re.sub(r'^www\.', '', m.group(1)) if m else ""


def _local_find_domain(company: str) -> str | None:
    """SearXNG + local LLM: pick the official domain from search results (must appear in a result URL)."""
    results = _searx(f"{company} official website")
    hosts = {_host(r["url"]) for r in results}
    listing = "\n".join(f"{i+1}. {r['title']} | {_host(r['url'])} | {r['content'][:150]}" for i, r in enumerate(results))
    data = _fallback_llm_json(
        "You identify official company websites from search results. Reply with JSON only.",
        f'Which result is the official website of "{company}" (the company itself, not Wikipedia, '
        f'LinkedIn, a directory, a news site or a job board)?\n\n{listing}\n\n'
        f'Reply as {{"domain": "example.com"}} using a domain exactly as listed, or {{"domain": null}} if none is clearly official.',
        max_tokens=100,
    )
    domain = _clean_domain(str(data.get("domain") or ""))
    if domain and any(h == domain or h.endswith("." + domain) for h in hosts):
        return domain
    return None


def _local_search_contacts(company: str, job_title: str, domain: str | None) -> dict:
    """SearXNG + local LLM leadership extraction. Only names present in the gathered text are kept."""
    queries = [f"{company} leadership team", f"{company} CEO chief executive officer"]
    if domain:
        queries.append(f"{company} {domain} executive team")
    queries.append(f"{company} chief data officer chief people officer head of talent")
    blocks, seen = [], set()
    for q in queries:
        try:
            results = _searx(q)
        except ResearchError:
            if q == queries[0]:
                raise
            continue
        for r in results:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            blocks.append(f"[{r['title']}] ({r['url']})\n{r['content']}")
    context = "\n\n".join(blocks)
    # Fetch up to 2 leadership-looking result pages (snippets alone are thin).
    pages = 0
    for u in [u for u in seen if (re.search(r'leader|executive|management|our-team|/team|people', u.lower())
               and (not domain or _host(u).endswith(domain))) or "en.wikipedia.org/wiki/" in u]:
        if pages >= 2:
            break
        try:
            r = requests.get(u, headers=HEADERS, timeout=FETCH_TIMEOUT)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = re.sub(r'\s+', ' ', soup.get_text(separator=" ", strip=True))
            if len(text) > 300:
                context += f"\n\n[Page] ({u})\n{text[:4000]}"
                pages += 1
        except Exception as e:
            logger.debug(f"Fallback page fetch failed {u}: {e}")
    if domain:
        page_text, page_url = _try_scrape(domain)
        if page_url:
            context += f"\n\n[Company leadership page] ({page_url})\n{page_text}"
    context = context[:16000]

    data = _fallback_llm_json(
        "You extract people from search results. Use ONLY names and titles that appear in the text provided. "
        "Never invent or guess a person. Reply with JSON only.",
        f'Company: "{company}"{" (" + domain + ")" if domain else ""}\n\n{context}\n\n'
        f'List current leadership and key people at this company who matter to a {job_title or "Director of Data Analytics"} '
        f'candidate (CEO/President, data/analytics leaders, HR/talent leaders). Max 6, most relevant first. Reply as '
        f'{{"company_summary": "1-2 sentences, only facts in the text", "contacts": [{{"name": "Full Name", '
        f'"title": "Title as written", "notes": "why relevant, 1 sentence", "confidence": "high|medium|low"}}]}}',
        max_tokens=1500,
    )
    haystack = re.sub(r'\s+', ' ', context.lower())
    contacts = []
    for c in data.get("contacts") or []:
        name = re.sub(r'\s+', ' ', str(c.get("name", ""))).strip()
        if len(name.split()) >= 2 and name.lower() in haystack:
            c["name"] = name
            contacts.append(c)
        else:
            logger.info(f"Fallback dropped contact not found in search text: {name!r}")
    return {"company_summary": str(data.get("company_summary") or ""), "contacts": contacts}


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


# ── Step 4: Google email search (per-contact, parallel) ───────────────────────

def _google_email_search(name: str, company: str) -> str | None:
    """
    Use Claude web search to find a contact's email address via Google.
    Mirrors the manual technique: search '{name}' '{company}' 'email address'.
    Returns the email string if found, or None.
    """
    logger.info(f"Google email search: {name} @ {company}")
    try:
        result = _claude_search(
            f'Search Google for the professional email address of "{name}" at "{company}". '
            f'Try queries like: "{name}" "{company}" email, site:linkedin.com "{name}" "{company}", '
            f'"{name}" contact email. '
            f'If you find a real professional email address for this specific person, '
            f'return ONLY the email address (e.g. jsmith@company.com). '
            f'If you cannot find one with confidence, return exactly: NONE',
            max_tokens=100
        )
        result = result.strip()
        # Extract email from result
        match = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', result)
        if match:
            email = match.group().lower()
            # Filter out generic/placeholder emails
            skip = ['noreply', 'example.com', 'info@', 'support@', 'contact@',
                    'hello@', 'admin@', 'test@', 'none@']
            if not any(g in email for g in skip):
                logger.info(f"Found email for {name}: {email}")
                return email
    except Exception as e:
        logger.error(f"Google email search failed for {name} @ {company}: {e}")
    return None


def _build_google_email_url(name: str, company: str) -> str:
    """Build a Google search URL for finding a contact's email manually."""
    query = requests.utils.quote(f'"{name}" "{company}" email address')
    return f"https://www.google.com/search?q={query}"


def _enrich_contacts_with_emails(contacts: list[dict], company: str) -> list[dict]:
    """
    Run Google email searches in parallel for all contacts.
    Adds 'email' (if found) and 'google_email_search_url' (always) to each contact.
    """
    if not contacts:
        return contacts

    def _search_one(idx_contact):
        idx, c = idx_contact
        name = c.get("name", "")
        if not name:
            return idx, c
        # Always add the fallback Google search URL
        c["google_email_search_url"] = _build_google_email_url(name, company)
        # Only run the live search if we don't already have an email
        if not c.get("email"):
            found = _google_email_search(name, company)
            if found:
                c["email"] = found
        return idx, c

    # Run up to 4 searches in parallel (respects rate limits while being fast)
    results = [None] * len(contacts)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_search_one, (i, c)): i for i, c in enumerate(contacts)}
        for future in as_completed(futures):
            try:
                idx, enriched = future.result()
                results[idx] = enriched
            except Exception as e:
                logger.error(f"Email enrichment future failed: {e}")
                results[futures[future]] = contacts[futures[future]]

    return results


# ── Main entry point ───────────────────────────────────────────────────────────

def research_company(company: str, job_title: str = "", user_id: str = "") -> dict:
    """
    Research a company to find key contacts.
    Primary: Claude web search. Bonus: direct site scrape + Google email search.
    """
    logger.info(f"=== Researching: {company} ===")
    result = {
        "company_name": company,
        "contacts": [],
        "company_summary": "",
        "source_url": "",
        "searches_run": [],
    }

    # Claude web search is primary; on ResearchError fall back to SearXNG + local LLM.
    claude_down = False
    fallback_used = False

    def _with_fallback(label, primary, local):
        nonlocal claude_down, fallback_used
        if not claude_down:
            try:
                return primary()
            except ResearchError as e:
                claude_down = True
                primary_err = e
                logger.warning(f"{label}: Claude search failed ({e}); trying SearXNG + local LLM")
                result["searches_run"].append(f"Claude web search failed ({e}); using SearXNG + local LLM fallback")
                try:
                    value = local()
                except ResearchError as e2:
                    raise ResearchError(f"{primary_err}; local fallback also failed: {e2}") from e2
                fallback_used = True
                return value
        try:
            value = local()
        except ResearchError as e2:
            raise ResearchError(f"Claude search unavailable; local fallback also failed: {e2}") from e2
        fallback_used = True
        return value

    # Step 1: Find domain
    domain = _with_fallback("domain", lambda: _find_domain(company), lambda: _local_find_domain(company))
    tag = " [SearXNG+local LLM]" if fallback_used else ""
    result["searches_run"].append(
        f"Domain search{tag}: {'found ' + domain if domain else 'not found'}"
    )
    logger.info(f"Domain: {domain}")

    # Step 2: Web search for contacts (primary — always runs)
    extracted = _with_fallback(
        "contacts",
        lambda: _search_contacts(company, job_title, domain),
        lambda: _local_search_contacts(company, job_title, domain),
    )
    result["company_summary"] = extracted.get("company_summary", "")
    raw_contacts = extracted.get("contacts", [])
    result["searches_run"].append(
        f"Leadership web search{' [SearXNG+local LLM]' if claude_down else ''}: {len(raw_contacts)} contacts found"
    )
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
            "google_email_search_url": "",  # populated in step 5
            "notes": c.get("notes", ""),
            "confidence": c.get("confidence", "medium"),
        })

    # Step 5: Google email search (parallel, non-blocking per contact). Skipped when Claude is down.
    if contacts and claude_down:
        for c in contacts:
            c["google_email_search_url"] = _build_google_email_url(c["name"], company)
        result["searches_run"].append("Google email search: skipped (Claude unavailable; manual search links added)")
    elif contacts:
        contacts = _enrich_contacts_with_emails(contacts, company)
        found_emails = sum(1 for c in contacts if c.get("email"))
        result["searches_run"].append(
            f"Google email search: {found_emails}/{len(contacts)} emails found"
        )
        logger.info(f"Email search: {found_emails}/{len(contacts)} found")

    result["contacts"] = contacts
    logger.info(
        f"=== Done: {company} | {len(contacts)} contacts | "
        f"source: {result['source_url'] or 'web search only'} ==="
    )
    return result
