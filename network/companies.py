"""Deterministic company keys, explicit aliases, and URL host extraction."""
import re
import unicodedata
from collections.abc import Mapping
from urllib.parse import urlsplit

_SUFFIXES = {"inc", "incorporated", "llc", "corp", "corporation", "ltd", "limited", "gmbh"}


def normalize_company(name: str, aliases: Mapping[str, str] | None = None) -> str:
    value = unicodedata.normalize("NFKC", name).casefold().strip()
    # Collapse dotted legal abbreviations before removing other punctuation.
    value = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m[0].replace(".", ""), value)
    tokens = re.sub(r"[^\w\s]", " ", value).split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    key = " ".join(tokens)
    if aliases:
        normalized = {normalize_company(k): normalize_company(v) for k, v in aliases.items()}
        key = normalized.get(key, key)
    return key


def extract_domain(value: str) -> str:
    """Return a normalized host, not a guessed registrable domain; no DNS calls."""
    value = value.strip()
    if not value or any(c.isspace() for c in value):
        return ""
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return ""
        if parsed.username or parsed.password:
            return ""
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if host.startswith("www."):
            host = host[4:]
        if "." not in host or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", p) for p in host.split(".")):
            return ""
        return host
    except (ValueError, UnicodeError):
        return ""
