"""Submission eligibility shared by both queues and the executor's recheck."""

from urllib.parse import parse_qsl, urlencode, urlsplit

from models import Job, usable_apply_url


def posting_keys(job: Job) -> set[tuple[str, ...]]:
    keys = {("id", job.id)}
    company = " ".join(job.company.casefold().split())
    identity = job.posting_identity
    if company and identity:
        keys.add(("requisition", company, identity.casefold()))
    for candidate in (job.ats_url, job.url):
        url = usable_apply_url(candidate)
        if not url:
            continue
        parsed = urlsplit(url)
        query = sorted(
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"trk", "refid"}
        )
        port = parsed.port
        if (parsed.scheme, port) in {("https", 443), ("http", 80)}:
            port = None
        keys.add(("url", parsed.hostname, str(port or ""),
                  parsed.path.rstrip("/"), urlencode(query), parsed.fragment))
    return keys


def application_blockers(job: Job, archived_keys: set[tuple[str, ...]]) -> list[str]:
    reasons = job.application_blockers()
    if not archived_keys.isdisjoint(posting_keys(job)):
        reasons.append("archived_or_duplicate")
    return reasons
