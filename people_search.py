"""
People search for company research: find people at a company who matter for a specific job.

Each account (tenant) chooses its own sources and keys in Settings → People Search:
  - pdl     People Data Labs Person Search   (free tier: 100 records/month; 1 credit per record returned)
  - hunter  Hunter.io Domain Search          (free tier: 50 credits/month; returns emails with names/titles)
  - apollo  Apollo.io People API Search      (paid Apollo plans only; search is free, Reveal costs a credit)

There is deliberately no server-wide fallback key: one account's searches never spend another's credits.
Results are cached per account so researching several jobs at one company costs credits once.
Nothing here logs names, emails or keys.
"""

import hashlib
import json
import logging
import re
import time

import requests

import storage

logger = logging.getLogger(__name__)

PROVIDERS = ("pdl", "hunter", "apollo")
LABELS = {"pdl": "People Data Labs", "hunter": "Hunter", "apollo": "Apollo"}
TIMEOUT = 20

PDL_URL = "https://api.peopledatalabs.com/v5/person/search"
HUNTER_URL = "https://api.hunter.io/v2"
APOLLO_URL = "https://api.apollo.io/api/v1"


class PeopleSearchError(RuntimeError):
    """A source failed (bad key, out of credits, rate limited, unreachable). Message is safe to show users."""


# ── Configuration ──────────────────────────────────────────────────────────────

def api_key(config, provider: str) -> str:
    return (getattr(config, f"{provider}_api_key", "") or "").strip()


def configured_providers(config) -> list[str]:
    """The account's chosen sources, in its order, that have a key."""
    out = []
    for p in getattr(config, "people_search_providers", None) or []:
        if p in PROVIDERS and p not in out and api_key(config, p):
            out.append(p)
    return out


# ── What to look for, from the job title ───────────────────────────────────────

_LEVEL_WORDS = {
    "senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate", "assistant", "executive",
    "director", "head", "vp", "svp", "evp", "avp", "vice", "president", "chief", "officer", "manager", "mgr",
    "of", "the", "and", "for", "in", "a", "an", "i", "ii", "iii", "iv", "remote", "hybrid", "contract",
}
_RECRUITING = ["talent acquisition", "recruiter", "recruiting"]


def job_function(job_title: str) -> str:
    """'Senior Director of Data Analytics (Remote)' -> 'data analytics'."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (job_title or "").lower())
    words = [w for w in re.split(r"[^a-z0-9+#]+", t) if w and w not in _LEVEL_WORDS]
    return " ".join(words[:4])


class Targets:
    """Who to look for at one company for one job.

    Nothing here assumes a kind of job: the starting point is always the job's own title, plus the account's
    own extra titles and its choice about recruiters (Settings -> People Search).
    """

    def __init__(self, job_title: str = "", extra_titles=(), recruiters: bool = True):
        self.job_title = job_title or ""
        self.function = job_function(self.job_title)
        self.extra = []
        for t in extra_titles or ():
            t = re.sub(r"\s+", " ", str(t)).strip().lower()
            if t and t not in self.extra:
                self.extra.append(t)
        self.extra = self.extra[:20]
        self.recruiters = bool(recruiters)

    def titles(self) -> list[str]:
        """Titles of likely hiring managers (from the job's own function), the account's extra titles, recruiters."""
        fn = self.function
        out = [f"head of {fn}", f"director of {fn}", f"vp {fn}", f"vice president {fn}", f"{fn} manager"] if fn else []
        out += self.extra
        return out + (_RECRUITING if self.recruiters else [])

    def key(self) -> str:
        return "|".join([self.function, ",".join(sorted(self.extra)), "r" if self.recruiters else ""])


def targets_for(config, job_title: str) -> Targets:
    return Targets(job_title, getattr(config, "people_search_titles", None) or [],
                   getattr(config, "people_search_include_recruiters", True))


def _as_targets(t) -> Targets:
    return t if isinstance(t, Targets) else Targets(t)


def target_titles(job_title) -> list[str]:
    return _as_targets(job_title).titles()


def _is_recruiting(title: str) -> bool:
    t = (title or "").lower()
    return any(r in t for r in _RECRUITING) or bool(re.search(r"\b(talent|people|hr|human resources)\b", t))


def relevance(title: str, targets) -> int:
    """Rank people: the job's own function or an extra title the account asked for, then seniority, then recruiting."""
    t, tg = (title or "").lower(), _as_targets(targets)
    fn_words = set(tg.function.split())
    score = 0
    if fn_words and fn_words & set(re.split(r"[^a-z0-9+#]+", t)):
        score += 3
    if any(x in t for x in tg.extra):
        score += 3
    if re.search(r"\b(chief|president|vp|vice president|head|director|svp|evp|partner|owner|founder)\b", t):
        score += 2
    if tg.recruiters and _is_recruiting(t):
        score += 2
    return score


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _request(provider: str, method: str, url: str, **kw) -> dict:
    try:
        r = requests.request(method, url, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        raise PeopleSearchError(f"could not reach {LABELS[provider]} ({type(e).__name__})") from e
    if r.status_code == 404 and provider == "pdl":
        return {"data": [], "total": 0}            # PDL answers "no records found" with 404
    if r.status_code in (401, 403):
        detail = _error_detail(r)
        if provider == "apollo" and r.status_code == 403:
            raise PeopleSearchError("this Apollo key or plan can't use people search (Apollo's free plan has no API access)")
        raise PeopleSearchError(f"key rejected by {LABELS[provider]}" + (f": {detail}" if detail else ""))
    if r.status_code == 402:
        raise PeopleSearchError(f"{LABELS[provider]} account is out of credits")
    if r.status_code == 429:
        raise PeopleSearchError(f"{LABELS[provider]} rate or usage limit reached; try later")
    if r.status_code >= 400:
        detail = _error_detail(r)
        raise PeopleSearchError(f"{LABELS[provider]} error {r.status_code}" + (f": {detail}" if detail else ""))
    try:
        return r.json()
    except ValueError as e:
        raise PeopleSearchError(f"{LABELS[provider]} returned an unreadable reply") from e


def _error_detail(r) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "")[:160]
    if isinstance(err, str):
        return err[:160]
    errs = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errs, list) and errs and isinstance(errs[0], dict):
        return " ".join(str(errs[0].get(k) or "") for k in ("id", "details")).strip()[:160]
    return ""


def _li(url) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    url = url.strip()
    return url if url.startswith("http") else "https://" + url


def _str(v) -> str:
    """PDL's free plan returns `true` in place of hidden contact fields; only keep real strings."""
    return v.strip() if isinstance(v, str) else ""


# ── Sources ────────────────────────────────────────────────────────────────────

def _pdl_search(key: str, company: str, domain: str | None, targets, limit: int) -> list[dict]:
    tg = _as_targets(targets)
    company_clause = ({"term": {"job_company_website": domain}} if domain
                      else {"match_phrase": {"job_company_name": company.lower()}})
    # job_title is an exact-value field; job_title.text is the word-searchable copy.
    # PDL sorts matches by profile completeness, not relevance, so leaders and recruiters are two searches:
    # otherwise one group can crowd out the other. Together they never return more than `limit` people.
    limit = max(1, min(limit, 100))
    leader_clauses = []
    if tg.function:
        # Every word of the job's function in the title, any order ("VP, Analytics & Data" matches), at a senior level.
        leader_clauses.append({"bool": {"must": [{"match": {"job_title.text": w}} for w in tg.function.split()] + [
            {"terms": {"job_title_levels": ["cxo", "vp", "director", "manager", "owner", "partner"]}}]}})
    leader_clauses += [{"match_phrase": {"job_title.text": x}} for x in tg.extra]
    leaders_q = {"bool": {"should": leader_clauses}} if leader_clauses else None
    recruiters_q = ({"bool": {"should": [{"match_phrase": {"job_title.text": r}} for r in _RECRUITING]}}
                    if tg.recruiters else None)
    searches = []
    if leaders_q and recruiters_q:
        n_leaders = limit if limit < 3 else limit - max(1, limit // 3)
        searches.append((leaders_q, n_leaders))
        if limit - n_leaders:
            searches.append((recruiters_q, limit - n_leaders))
    elif leaders_q or recruiters_q:
        searches.append((leaders_q or recruiters_q, limit))
    records = []
    for title_q, size in searches:
        body = {"query": {"bool": {"must": [company_clause, title_q]}}, "size": size, "titlecase": True}
        records += _request("pdl", "POST", PDL_URL, json=body, headers={"X-Api-Key": key}).get("data") or []
    data = {"data": records}
    out, seen = [], set()
    for p in data.get("data") or []:
        name = _str(p.get("full_name"))
        if not name or (p.get("id") and p.get("id") in seen):
            continue
        seen.add(p.get("id"))
        out.append({
            "name": name, "title": _str(p.get("job_title")),
            "email": _str(p.get("work_email")), "linkedin_url": _li(p.get("linkedin_url")),
            "source_id": _str(p.get("id")),
        })
    return out


def _hunter_search(key: str, company: str, domain: str | None, targets, limit: int) -> list[dict]:
    # Hunter can't filter by title: it returns the company's known people, ranked afterwards by relevance().
    params = {"type": "personal", "limit": max(1, min(limit, 100))}
    params["domain" if domain else "company"] = domain or company
    try:
        data = _request("hunter", "GET", f"{HUNTER_URL}/domain-search", params=params, headers={"X-API-KEY": key})
    except PeopleSearchError as e:
        if "pagination" not in str(e).lower() or params["limit"] <= 10:
            raise
        params = {**params, "limit": 10}             # free plan: limit + offset must be <= 10
        data = _request("hunter", "GET", f"{HUNTER_URL}/domain-search", params=params, headers={"X-API-KEY": key})
    out = []
    for e in (data.get("data") or {}).get("emails") or []:
        name = " ".join(x for x in (_str(e.get("first_name")), _str(e.get("last_name"))) if x)
        if len(name.split()) < 2:
            continue
        conf = e.get("confidence")
        out.append({
            "name": name, "title": _str(e.get("position")), "email": _str(e.get("value")),
            "linkedin_url": _li(e.get("linkedin")),
            "email_confidence": int(conf) if isinstance(conf, (int, float)) else None,
        })
    return out


def _apollo_search(key: str, company: str, domain: str | None, targets, limit: int) -> list[dict]:
    titles = _as_targets(targets).titles()
    if not titles:
        return []
    params = [("per_page", max(1, min(limit, 100))), ("page", 1)]
    params += [("person_titles[]", t) for t in titles]
    params += [("person_seniorities[]", s) for s in ("c_suite", "vp", "head", "director", "manager")]
    if domain:
        params.append(("q_organization_domains_list[]", domain))
    else:
        params.append(("q_keywords", company))
    data = _request("apollo", "POST", f"{APOLLO_URL}/mixed_people/api_search", params=params,
                    headers={"x-api-key": key, "Cache-Control": "no-cache", "Content-Type": "application/json"})
    out = []
    for p in data.get("people") or []:
        first = _str(p.get("first_name"))
        last = _str(p.get("last_name")) or _str(p.get("last_name_obfuscated"))
        if not first or not p.get("id"):
            continue
        out.append({
            "name": f"{first} {last}".strip(), "title": _str(p.get("title")),
            "source_id": str(p["id"]), "masked": "*" in last or not last,
        })
    return out


_SEARCH = {"pdl": _pdl_search, "hunter": _hunter_search, "apollo": _apollo_search}


def apollo_reveal(config, person_id: str) -> dict:
    """Spend one Apollo credit to get a masked person's full name, email and LinkedIn. Raises PeopleSearchError."""
    key = api_key(config, "apollo")
    if not key:
        raise PeopleSearchError("no Apollo key is saved for this account")
    data = _request("apollo", "POST", f"{APOLLO_URL}/people/match",
                    params={"id": person_id, "reveal_personal_emails": "false"},
                    headers={"x-api-key": key, "Cache-Control": "no-cache", "Content-Type": "application/json"})
    p = data.get("person") or {}
    if not p:
        raise PeopleSearchError("Apollo could not find this person any more")
    name = _str(p.get("name")) or " ".join(x for x in (_str(p.get("first_name")), _str(p.get("last_name"))) if x)
    email = _str(p.get("email"))
    if "email_not_unlocked" in email or "domain.com" in email:
        email = ""
    return {"name": name, "title": _str(p.get("title")), "email": email, "linkedin_url": _li(p.get("linkedin_url"))}


def check_key(config, provider: str) -> str:
    """Confirm a source accepts the saved key without spending credits. Returns a short status line."""
    if provider not in PROVIDERS:
        raise PeopleSearchError("unknown source")
    key = api_key(config, provider)
    if not key:
        raise PeopleSearchError(f"no {LABELS[provider]} key is saved for this account")
    if provider == "hunter":
        d = (_request("hunter", "GET", f"{HUNTER_URL}/account", headers={"X-API-KEY": key}).get("data") or {})
        plan = d.get("plan_name") or "unknown plan"
        cr = ((d.get("requests") or {}).get("credits") or (d.get("requests") or {}).get("searches") or {})
        left = f"; {cr.get('used', '?')} of {cr.get('available', '?')} credits used this month" if cr else ""
        return f"key works ({plan}{left})"
    if provider == "apollo":
        d = _request("apollo", "GET", f"{APOLLO_URL}/auth/health", headers={"x-api-key": key})
        if not d.get("is_logged_in", d.get("healthy")):
            raise PeopleSearchError("Apollo did not accept the key")
        # The health check passes on free plans too; people search is what free plans lack, so try it (no credits).
        _request("apollo", "POST", f"{APOLLO_URL}/mixed_people/api_search",
                 params=[("per_page", 1), ("q_organization_domains_list[]", "apollo.io")],
                 headers={"x-api-key": key, "Cache-Control": "no-cache", "Content-Type": "application/json"})
        return "key works and can use people search"
    # PDL bills per record returned, so search for something that cannot match: costs nothing, still checks the key.
    _request("pdl", "POST", PDL_URL, json={"query": {"term": {"id": "jshq-key-check-no-match"}}, "size": 1},
             headers={"X-Api-Key": key})
    return "key works"


# ── Cache (per account) ────────────────────────────────────────────────────────

def _cache_path(user_id: str):
    return storage.get_user_dir(user_id) / "people_search_cache.json"


def _cache_key(provider: str, company: str, domain: str | None, targets: Targets, limit: int) -> str:
    raw = "|".join([provider, (domain or company or "").lower().strip(), targets.key(), str(limit)])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _cache_get(user_id: str, key: str, max_age_days: int):
    if not user_id or max_age_days <= 0:
        return None
    try:
        entry = json.loads(_cache_path(user_id).read_text()).get(key)
    except (OSError, ValueError):
        return None
    if entry and time.time() - entry.get("at", 0) < max_age_days * 86400:
        return entry.get("people")
    return None


def _cache_put(user_id: str, key: str, people: list[dict]) -> None:
    if not user_id:
        return
    path = _cache_path(user_id)
    try:
        cache = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        cache = {}
    cutoff = time.time() - 180 * 86400
    cache = {k: v for k, v in cache.items() if v.get("at", 0) > cutoff}
    cache[key] = {"at": time.time(), "people": people}
    try:
        storage.ensure_user_dirs(user_id)
        path.write_text(json.dumps(cache))
    except OSError as e:
        logger.warning(f"People search cache not saved: {type(e).__name__}")


# ── Merge + entry point ────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def merge_contacts(existing: list[dict], found: list[dict]) -> list[dict]:
    """Add found people to existing contacts. Same full name = same person: fill in missing fields, add the source.
    Masked names (Apollo before Reveal) never merge."""
    out = [dict(c) for c in existing]
    by_name = {_norm(c.get("name", "")): c for c in out if not c.get("masked")}
    for f in found:
        n = _norm(f.get("name", ""))
        match = by_name.get(n) if n and not f.get("masked") else None
        if match is None:
            c = dict(f)
            c["sources"] = list(f.get("sources") or [])
            out.append(c)
            if n and not f.get("masked"):
                by_name[n] = c
            continue
        for field in ("title", "email", "linkedin_url"):
            if not match.get(field) and f.get(field):
                match[field] = f[field]
        for s in f.get("sources") or []:
            if s not in match.setdefault("sources", []):
                match["sources"].append(s)
        for p, pid in (f.get("provider_ids") or {}).items():
            match.setdefault("provider_ids", {})[p] = pid
    return out


def search_people(config, company: str, domain: str | None, job_title: str, user_id: str = "") -> tuple[list[dict], list[str]]:
    """Search the account's chosen sources. Never raises: failures become notes for searches_run.

    Returns (contacts, notes). Contacts are ranked by relevance to the job and capped at the account's maximum.
    """
    providers = configured_providers(config)
    if not providers:
        return [], []
    limit = max(1, min(int(getattr(config, "people_search_max_results", 8) or 8), 25))
    cache_days = int(getattr(config, "people_search_cache_days", 30) or 0)
    stop_at_first = (getattr(config, "people_search_mode", "first") or "first") == "first"
    tg = targets_for(config, job_title)
    people: list[dict] = []
    notes: list[str] = []
    for p in providers:
        ck = _cache_key(p, company, domain, tg, limit)
        found = _cache_get(user_id, ck, cache_days)
        cached = found is not None
        if not cached:
            try:
                found = _SEARCH[p](api_key(config, p), company, domain, tg, limit)
            except PeopleSearchError as e:
                notes.append(f"People search ({LABELS[p]}): failed ({e})")
                continue
            _cache_put(user_id, ck, found)
        for f in found:
            f["sources"] = [p]
            if f.get("source_id"):
                f["provider_ids"] = {p: f.pop("source_id")}
        notes.append(f"People search ({LABELS[p]}{', saved result' if cached else ''}): {len(found)} found")
        people = merge_contacts(people, found)
        if found and stop_at_first:
            break
    people.sort(key=lambda c: relevance(c.get("title", ""), tg), reverse=True)
    contacts = []
    for c in people[:limit]:
        contacts.append({
            "name": c.get("name", ""), "title": c.get("title", ""), "email": c.get("email", ""),
            "linkedin_url": c.get("linkedin_url", ""),
            "notes": _why(c.get("title", ""), tg),
            "confidence": "high" if c.get("email") or c.get("linkedin_url") else "medium",
            "sources": c.get("sources", []), "provider_ids": c.get("provider_ids", {}),
            "masked": bool(c.get("masked")),
        })
    return contacts, notes


def _why(title: str, tg: Targets) -> str:
    t = (title or "").lower()
    fn_words = set(tg.function.split())
    if fn_words and fn_words & set(re.split(r"[^a-z0-9+#]+", t)):
        return "Works in the same area as this job"
    if any(x in t for x in tg.extra):
        return "Matches a title you look for"
    if _is_recruiting(t):
        return "Recruiting / people team"
    return ""
