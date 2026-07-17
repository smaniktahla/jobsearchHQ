"""
Company career-site search.

This is intentionally separate from JobSpy because JobSpy only supports a fixed
set of aggregator boards. Company career pages vary wildly, so this module uses
best-effort discovery: known ATS URL normalization, JSON-LD JobPosting data,
and job-like links from the supplied site.
"""

import json
import logging
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

import scoring
import storage
from models import IntakeSource, Job, JobStatus, MarketLane

logger = logging.getLogger(__name__)

JOB_LINK_PATTERNS = (
    "job",
    "jobs",
    "career",
    "careers",
    "opening",
    "position",
    "requisition",
    "req",
    "apply",
)

NOISE_PATTERNS = (
    "privacy",
    "terms",
    "cookie",
    "login",
    "signin",
    "talentcommunity",
    "savedjobs",
    "profile",
)

JOB_PAGE_USER_AGENTS = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
)

REAL_JD_MARKERS = (
    "responsibilities",
    "basic qualifications",
    "preferred qualifications",
    "salary compensation",
    "work location type",
    "clearance",
    "employment type",
)


class CompanySiteRequest(BaseModel):
    company: str = ""
    url: str


@dataclass
class CandidateJob:
    url: str
    title: str = ""
    company: str = ""
    raw_jd: str = ""
    location: str = ""
    pay_range: str = ""
    extraction_method: str = ""


@dataclass
class FetchAttempt:
    user_agent: str
    status_code: int = 0
    html_length: int = 0
    has_jsonld: bool = False
    has_jobposting: bool = False
    has_unavailable: bool = False
    title: str = ""
    raw_jd_length: int = 0
    accepted: bool = False
    error: str = ""


def search_company_sites(
    sites: list[CompanySiteRequest],
    search_term: str,
    user_id: str,
    results_wanted: int = 25,
    auto_score: bool = False,
    skip_existing: bool = True,
) -> dict:
    result = {"created": [], "skipped": [], "errors": [], "total_scraped": 0}
    all_existing = storage.load_all_jobs(user_id) if skip_existing else []

    for site in sites:
        try:
            created, skipped, errors, total = _search_one_site(
                site=site,
                search_term=search_term,
                user_id=user_id,
                results_wanted=results_wanted,
                auto_score=auto_score,
                skip_existing=skip_existing,
                all_existing=all_existing,
            )
            result["created"].extend(created)
            result["skipped"].extend(skipped)
            result["errors"].extend(errors)
            result["total_scraped"] += total
        except Exception as e:
            logger.error("Company site search failed for %s: %s", site.url, e, exc_info=True)
            result["errors"].append({
                "source": site.company or site.url,
                "error": str(e),
            })

    return result


def _search_one_site(
    site: CompanySiteRequest,
    search_term: str,
    user_id: str,
    results_wanted: int,
    auto_score: bool,
    skip_existing: bool,
    all_existing: list[Job],
) -> tuple[list[dict], list[dict], list[dict], int]:
    created: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    company = site.company.strip() or _company_from_host(site.url)
    seed_urls = _seed_urls(site.url, search_term)

    with httpx.Client(follow_redirects=True, timeout=20, headers=_headers()) as client:
        candidate_urls: list[str] = []
        for seed in seed_urls:
            try:
                html = _fetch(client, seed)
                candidate_urls.extend(_extract_job_urls(seed, html))
                candidate_urls.extend(j.url for j in _extract_jsonld_jobs(seed, html))
            except Exception as e:
                errors.append({"source": company, "url": seed, "error": f"Discovery failed: {e}"})

        candidate_urls = _unique_same_site(candidate_urls, site.url)
        total_scraped = len(candidate_urls)

        for url in candidate_urls[: max(results_wanted * 3, results_wanted)]:
            if len(created) >= results_wanted:
                break
            try:
                job = _fetch_best_job_page(url, company)
                if not job.title and not job.raw_jd:
                    continue
                if not _matches_search(job, search_term):
                    continue

                existing = _find_existing(all_existing, job.title, job.company, job.url) if skip_existing else None
                if existing:
                    skipped.append({
                        "id": existing.id,
                        "title": job.title,
                        "company": job.company,
                        "reason": "duplicate",
                    })
                    continue

                saved = Job(
                    title=job.title,
                    company=job.company or company,
                    url=job.url,
                    source=f"company_site_{urlparse(site.url).netloc}",
                    intake_source=IntakeSource.API,
                    raw_jd=job.raw_jd,
                    pay_range=job.pay_range,
                    market_lane=MarketLane.CONTRACT,
                    notes=" | ".join(p for p in [
                        "Source: company site",
                        f"Career site: {site.url}",
                        f"Location: {job.location}" if job.location else "",
                    ] if p),
                )

                storage.save_job(user_id, saved)
                all_existing.append(saved)

                score_val = None
                if auto_score and len(saved.raw_jd) > 200:
                    try:
                        score_result = scoring.score_job(saved, user_id)
                        saved.score = score_result
                        saved.update_status(JobStatus.SCORED)
                        if score_result.recommended_lane:
                            saved.market_lane = score_result.recommended_lane
                        storage.save_job(user_id, saved)
                        score_val = score_result.total
                    except Exception as e:
                        errors.append({
                            "title": saved.title,
                            "company": saved.company,
                            "error": f"scoring failed: {e}",
                        })

                created.append({
                    "id": saved.id,
                    "title": saved.title,
                    "company": saved.company,
                    "location": job.location,
                    "pay_range": saved.pay_range,
                    "source": "company_site",
                    "job_url": saved.url,
                    "has_description": len(saved.raw_jd) > 200,
                    "score": score_val,
                })
            except Exception as e:
                errors.append({"source": company, "url": url, "error": str(e)})

    return created, skipped, errors, total_scraped


def _find_existing(all_jobs: list[Job], title: str, company: str, url: str) -> Job | None:
    for job in all_jobs:
        if url and job.url and url.rstrip("/").lower() == job.url.rstrip("/").lower():
            return job
        if (
            title and company
            and title.lower().strip() == job.title.lower().strip()
            and company.lower().strip() == job.company.lower().strip()
        ):
            return job
    return None


def _headers(user_agent: str | None = None) -> dict[str, str]:
    return {
        "User-Agent": user_agent or JOB_PAGE_USER_AGENTS[0],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def _fetch(client: httpx.Client, url: str) -> str:
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def _fetch_best_job_page(url: str, fallback_company: str) -> CandidateJob:
    job, _attempts = _fetch_best_job_page_with_diagnostics(url, fallback_company)
    return job


def _fetch_best_job_page_with_diagnostics(url: str, fallback_company: str) -> tuple[CandidateJob, list[FetchAttempt]]:
    best: CandidateJob | None = None
    last_error: Exception | None = None
    attempts: list[FetchAttempt] = []
    for user_agent in JOB_PAGE_USER_AGENTS:
        attempt = FetchAttempt(user_agent=user_agent)
        try:
            with httpx.Client(follow_redirects=True, timeout=20, headers=_headers(user_agent)) as client:
                resp = client.get(url)
                attempt.status_code = resp.status_code
                resp.raise_for_status()
                html = resp.text
                attempt.html_length = len(html)
                lowered_html = html.lower()
                attempt.has_jsonld = "application/ld+json" in lowered_html
                attempt.has_jobposting = "jobposting" in lowered_html
                attempt.has_unavailable = _is_unavailable_text(html)
            job = _parse_job_page(url, html, fallback_company)
            attempt.title = job.title
            attempt.raw_jd_length = len(job.raw_jd)
            if _has_real_job_description(job.raw_jd):
                attempt.accepted = True
                attempts.append(attempt)
                return job, attempts
            if best is None or len(job.raw_jd) > len(best.raw_jd):
                best = job
        except Exception as e:
            last_error = e
            attempt.error = str(e)
            continue
        finally:
            if not attempts or attempts[-1] is not attempt:
                attempts.append(attempt)
    if best:
        return best, attempts
    if last_error:
        raise last_error
    return CandidateJob(url=url, company=fallback_company), attempts


def _seed_urls(url: str, search_term: str) -> list[str]:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = urlparse("https://" + url.strip())

    seeds = [_clean_tracking_url(urlunparse(parsed))]
    term_qs = quote_plus(search_term.strip())
    host_base = f"{parsed.scheme}://{parsed.netloc}"
    path_parts = [p for p in parsed.path.split("/") if p]

    if "phenom" in parsed.netloc or "careers." in parsed.netloc:
        locale_prefix = "/".join(path_parts[:2]) if len(path_parts) >= 2 else ""
        prefix = f"/{locale_prefix}" if locale_prefix else ""
        seeds.extend([
            f"{host_base}{prefix}/search-results?keywords={term_qs}",
            f"{host_base}{prefix}/search-results",
            f"{host_base}{prefix}/jobs?keywords={term_qs}",
        ])

    seeds.extend([
        f"{host_base}/careers?{urlencode({'keywords': search_term})}",
        f"{host_base}/jobs?{urlencode({'keywords': search_term})}",
        f"{host_base}/search-results?{urlencode({'keywords': search_term})}",
        f"{host_base}/careers",
        f"{host_base}/jobs",
    ])
    return list(dict.fromkeys(seeds))


def _clean_tracking_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    keep = {
        key: vals
        for key, vals in qs.items()
        if not key.lower().startswith("utm_") and key.lower() not in {"applychannel", "step", "stepname"}
    }
    return urlunparse(parsed._replace(query=urlencode(keep, doseq=True)))


def _extract_job_urls(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        absolute = _clean_tracking_url(urljoin(base_url, href))
        lowered = absolute.lower()
        if any(n in lowered for n in NOISE_PATTERNS):
            continue
        link_text = link.get_text(" ", strip=True).lower()
        if any(p in lowered or p in link_text for p in JOB_LINK_PATTERNS):
            urls.append(absolute)

    urls.extend(_extract_urls_from_text(base_url, html))
    return urls


def _extract_urls_from_text(base_url: str, text: str) -> list[str]:
    urls: list[str] = []
    for raw in re.findall(r'https?:\\?/\\?/[^"\\\']+', text):
        cleaned = raw.replace("\\/", "/")
        cleaned = unescape(cleaned).split("\\u0026")[0]
        if any(p in cleaned.lower() for p in JOB_LINK_PATTERNS):
            urls.append(_clean_tracking_url(urljoin(base_url, cleaned)))
    return urls


def _extract_jsonld_jobs(base_url: str, html: str) -> list[CandidateJob]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[CandidateJob] = []
    for script in soup.find_all("script", type="application/ld+json"):
        payload = script.string or script.get_text()
        for item in _json_items(payload):
            if str(item.get("@type", "")).lower() != "jobposting":
                continue
            url = item.get("url") or base_url
            jobs.append(CandidateJob(
                url=_clean_tracking_url(urljoin(base_url, url)),
                title=str(item.get("title", "")).strip(),
                company=_org_name(item.get("hiringOrganization")),
                raw_jd=BeautifulSoup(unescape(str(item.get("description", ""))), "html.parser").get_text("\n", strip=True),
                location=_location_name(item.get("jobLocation")),
                pay_range=_pay_range(item),
            ))
    return jobs


def _json_items(payload: str) -> list[dict]:
    try:
        data = json.loads(payload)
    except Exception:
        return []
    queue = data if isinstance(data, list) else [data]
    items: list[dict] = []
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            items.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
        elif isinstance(item, list):
            queue.extend(item)
    return items


def _parse_job_page(url: str, html: str, fallback_company: str) -> CandidateJob:
    if "linkedin.com" in urlparse(url).netloc.lower():
        linkedin_job = _extract_linkedin_job(url, html, fallback_company)
        if linkedin_job and len(linkedin_job.raw_jd) > 200:
            return linkedin_job

    phenom_job = _extract_phenom_job(url, html, fallback_company)
    if phenom_job and len(phenom_job.raw_jd) > 200:
        return phenom_job

    jsonld = _extract_jsonld_jobs(url, html)
    if jsonld:
        job = jsonld[0]
        if job.raw_jd:
            job.extraction_method = "jsonld"
            return job

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    title = _first_text(soup, ["h1", "[data-ph-at-id='job-title']", ".job-title", ".jobTitle"])
    if not title:
        meta_title = soup.find("meta", property="og:title") or soup.find("title")
        title = meta_title.get("content", "") if meta_title and meta_title.has_attr("content") else ""
        if not title and meta_title:
            title = meta_title.get_text(" ", strip=True)
    title = re.sub(r"\s+", " ", title).strip(" -|")

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    location = _guess_location(text)

    return CandidateJob(
        url=_clean_tracking_url(url),
        title=title[:200],
        company=fallback_company,
        raw_jd=text[:12000],
        location=location,
        pay_range=_guess_pay(text),
        extraction_method="html_text",
    )


def _extract_linkedin_job(url: str, html: str, fallback_company: str) -> CandidateJob | None:
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.find(class_="description__text")
    if not desc:
        return None

    raw_jd = desc.get_text("\n", strip=True)
    raw_jd = re.sub(r"\n{3,}", "\n\n", raw_jd)

    title_tag = soup.find(class_="top-card-layout__title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    org_tag = soup.find(class_="topcard__org-name-link")
    company = org_tag.get_text(strip=True) if org_tag else fallback_company

    loc_tag = soup.find(class_="topcard__flavor--bullet")
    location = loc_tag.get_text(strip=True) if loc_tag else ""

    return CandidateJob(
        url=_clean_tracking_url(url),
        title=title[:200],
        company=company,
        raw_jd=raw_jd[:12000],
        location=location,
        pay_range=_guess_pay(raw_jd),
        extraction_method="linkedin_dom",
    )


def _is_unavailable_text(text: str) -> bool:
    lowered = text.lower().replace("…", "...")
    return (
        "job you are trying to apply for is no longer available" in lowered
        or "the job you are trying to apply for is no longer available" in lowered
    )


def _has_real_job_description(text: str) -> bool:
    lowered = text.lower()
    if len(text.strip()) < 300:
        return False
    if _is_unavailable_text(text):
        return any(marker in lowered for marker in REAL_JD_MARKERS)
    return True


def _extract_phenom_job(url: str, html: str, fallback_company: str) -> CandidateJob | None:
    blobs = _extract_json_blobs(html)
    for blob in blobs:
        for item in _walk_json(blob):
            if not isinstance(item, dict):
                continue
            title = _pick_string(item, ("title", "jobTitle", "job_title", "requisitionTitle"))
            description = _pick_string(item, (
                "description", "jobDescription", "job_description",
                "externalDescription", "fullDescription",
            ))
            job_id = _pick_string(item, ("jobId", "jobSeqNo", "reqId", "requisitionId"))
            if not title or not description:
                continue
            if "no longer available" in description.lower() and len(description) < 500:
                continue

            location = _pick_string(item, ("location", "jobLocation", "cityStateCountry", "primaryLocation"))
            category = _pick_string(item, ("category", "jobCategory", "jobFamily"))
            posted = _pick_string(item, ("postedDate", "datePosted", "jobPostedDate"))
            company = _pick_string(item, ("company", "companyName", "hiringOrganization")) or fallback_company
            pay_range = _pick_string(item, ("salary", "salaryRange", "payRange"))
            body = BeautifulSoup(unescape(description), "html.parser").get_text("\n", strip=True)
            parts = [
                title,
                f"Company: {company}" if company else "",
                f"Job ID: {job_id}" if job_id else "",
                f"Location: {location}" if location else "",
                f"Category: {category}" if category else "",
                f"Posted: {posted}" if posted else "",
                "",
                body,
            ]
            return CandidateJob(
                url=_clean_tracking_url(url),
                title=title.strip()[:200],
                company=company.strip(),
                raw_jd="\n".join(p for p in parts if p is not None).strip()[:12000],
                location=location.strip(),
                pay_range=pay_range.strip(),
                extraction_method="phenom_json",
            )
    return None


def _extract_json_blobs(html: str) -> list:
    blobs = []
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text or ("job" not in text.lower() and "requisition" not in text.lower()):
            continue
        for candidate in _balanced_json_candidates(text):
            try:
                blobs.append(json.loads(candidate))
            except Exception:
                continue
    return blobs


def _balanced_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    starts = [m.start() for m in re.finditer(r"[\[{]", text)]
    for start in starts[:80]:
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        depth = 0
        in_str = False
        escape = False
        for index in range(start, min(len(text), start + 250000)):
            ch = text[index]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    if "job" in candidate.lower() or "requisition" in candidate.lower():
                        candidates.append(candidate)
                    break
    return candidates


def _walk_json(value):
    queue = [value]
    while queue:
        current = queue.pop(0)
        yield current
        if isinstance(current, dict):
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)


def _pick_string(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _pick_string(value, ("name", "title", "value", "formatted"))
            if nested:
                return nested
        if isinstance(value, list):
            parts = []
            for part in value:
                if isinstance(part, str) and part.strip():
                    parts.append(part.strip())
                elif isinstance(part, dict):
                    nested = _pick_string(part, ("name", "title", "value", "formatted"))
                    if nested:
                        parts.append(nested)
            if parts:
                return ", ".join(parts)
    return ""


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            return node.get_text(" ", strip=True)
    return ""


def _matches_search(job: CandidateJob, search_term: str) -> bool:
    terms = [t.lower() for t in re.findall(r"[a-zA-Z0-9+#.]{3,}", search_term)]
    if not terms:
        return True
    haystack = f"{job.title}\n{job.raw_jd}".lower()
    return any(term in haystack for term in terms)


def _unique_same_site(urls: list[str], seed_url: str) -> list[str]:
    seed_host = urlparse(seed_url).netloc.lower()
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.lower() != seed_host:
            continue
        cleaned = urlunparse(parsed._replace(fragment=""))
        key = cleaned.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique


def _company_from_host(url: str) -> str:
    host = urlparse(url if "://" in url else "https://" + url).netloc
    parts = [p for p in host.split(".") if p not in {"www", "careers", "jobs"}]
    return parts[0].replace("-", " ").title() if parts else host


def _org_name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name", "")).strip()
    return ""


def _location_name(value) -> str:
    if isinstance(value, list):
        return "; ".join(_location_name(v) for v in value if _location_name(v))
    if not isinstance(value, dict):
        return ""
    address = value.get("address")
    if isinstance(address, dict):
        return ", ".join(str(address.get(k, "")).strip() for k in (
            "addressLocality", "addressRegion", "addressCountry"
        ) if address.get(k))
    return str(value.get("name", "")).strip()


def _pay_range(item: dict) -> str:
    base = item.get("baseSalary")
    if not isinstance(base, dict):
        return ""
    value = base.get("value", {})
    if not isinstance(value, dict):
        return ""
    min_value = value.get("minValue")
    max_value = value.get("maxValue")
    unit = value.get("unitText", "")
    if min_value and max_value:
        suffix = f"/{unit.lower()}" if unit else ""
        return f"${float(min_value):,.0f} - ${float(max_value):,.0f}{suffix}"
    return ""


def _guess_location(text: str) -> str:
    for pattern in (r"Location\s*:\s*([^\n]+)", r"Job Location\s*:\s*([^\n]+)"):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:120]
    return ""


def _guess_pay(text: str) -> str:
    match = re.search(r"\$\s?[0-9][0-9,]*(?:\.\d+)?\s*(?:-|to)\s*\$?\s?[0-9][0-9,]*(?:\.\d+)?", text)
    return match.group(0) if match else ""
