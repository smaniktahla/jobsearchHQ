"""Deterministic identity resolution. No model or network access."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from .companies import normalize_company
from .models import Contact


def url_key(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if host != "linkedin.com" or not parsed.path.casefold().startswith("/in/"):
            return ""
        return host + parsed.path.rstrip("/").casefold()
    except ValueError:
        return ""


def name_key(contact: Contact) -> tuple[str, str]:
    return tuple(" ".join(v.casefold().split()) for v in (contact.first_name, contact.last_name))


@dataclass(frozen=True)
class Resolution:
    action: str  # new, match, review
    candidate_ids: tuple[str, ...] = ()
    reason: str = "no_match"


def resolve(contact: Contact, existing: list[Contact]) -> Resolution:
    """Use strongest available evidence; conflicting stronger IDs require review."""
    url = url_key(contact.linkedin_url)
    email = contact.email.strip().casefold()
    name = name_key(contact)
    company = normalize_company(contact.company)
    levels = [
        ("url", [c for c in existing if url and url_key(c.linkedin_url) == url]),
        ("email", [c for c in existing if email and c.email.strip().casefold() == email]),
        ("name_employer", [c for c in existing if all(name) and company and name_key(c) == name
                            and normalize_company(c.company) == company]),
    ]
    for reason, candidates in levels:
        if not candidates:
            continue
        ids = tuple(sorted({c.id for c in candidates}))
        if len(ids) != 1:
            return Resolution("review", ids, "ambiguous_" + reason)
        candidate = candidates[0]
        conflict = bool(reason != "url" and url and url_key(candidate.linkedin_url) and url != url_key(candidate.linkedin_url))
        conflict |= reason == "name_employer" and bool(email and candidate.email and email != candidate.email.strip().casefold())
        return Resolution("review" if conflict else "match", ids, "conflicting_identity" if conflict else reason)
    # A name alone is only a suggestion for a human, never merge evidence.
    same_name = tuple(sorted(c.id for c in existing if all(name) and name_key(c) == name))
    if same_name:
        return Resolution("review", same_name, "name_only")
    return Resolution("new")
