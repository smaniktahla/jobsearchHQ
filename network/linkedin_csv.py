"""Bounded, offline LinkedIn Connections CSV preview parser.

Errors contain only locations and stable codes, never source contact values.
A limit failure rejects the entire preview so callers cannot commit a truncation.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from storage import sanitize_unicode
from .models import Contact

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50_000
_HEADERS = {
    "firstname": "first_name", "lastname": "last_name",
    "emailaddress": "email", "email": "email", "url": "linkedin_url",
    "linkedinurl": "linkedin_url", "profileurl": "linkedin_url",
    "company": "company", "companyname": "company", "position": "position",
    "title": "position", "connectedon": "connected_on", "connectiondate": "connected_on",
}


@dataclass(frozen=True)
class ParseIssue:
    row: int | None
    code: str


@dataclass
class ParseResult:
    contacts: list[Contact] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    total_rows: int = 0


def _key(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.casefold())


def parse_linkedin_csv(path: str | Path, *, max_bytes: int = MAX_BYTES,
                       max_rows: int = MAX_ROWS) -> ParseResult:
    """Read at most max_bytes + 1 bytes, including for non-regular inputs."""
    if max_bytes < 1 or max_rows < 1:
        raise ValueError("limits must be positive")
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError:
        return ParseResult(issues=[ParseIssue(None, "unreadable_file")])
    return parse_linkedin_bytes(data, max_bytes=max_bytes, max_rows=max_rows)


def parse_linkedin_bytes(data: bytes, *, max_bytes: int = MAX_BYTES,
                         max_rows: int = MAX_ROWS) -> ParseResult:
    if max_bytes < 1 or max_rows < 1:
        raise ValueError("limits must be positive")
    result = ParseResult()
    if len(data) > max_bytes:
        return ParseResult(issues=[ParseIssue(None, "size_limit")])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParseResult(issues=[ParseIssue(None, "invalid_encoding")])
    lines = text.splitlines(keepends=True)
    # LinkedIn exports may precede the CSV with a multi-line Notes: block.
    start = 0
    if lines and lines[0].lstrip().startswith("Notes:"):
        while start < len(lines):
            if "firstname" in _key(lines[start]) and "lastname" in _key(lines[start]):
                break
            start += 1
    reader = csv.reader(io.StringIO("".join(lines[start:]), newline=""), strict=True)
    try:
        header = next(reader, [])
        fields = [_HEADERS.get(_key(h)) for h in header]
        if not {"first_name", "last_name"}.issubset(fields) or len([f for f in fields if f]) != len(set(f for f in fields if f)):
            return ParseResult(issues=[ParseIssue(start + 1, "invalid_header")])
        for row in reader:
            line = start + reader.line_num
            if not row or not any(v.strip() for v in row):
                continue
            result.total_rows += 1
            if result.total_rows > max_rows:
                return ParseResult(issues=[ParseIssue(line, "row_limit")], total_rows=result.total_rows)
            if len(row) != len(header):
                result.issues.append(ParseIssue(line, "malformed_row"))
                continue
            values = {key: sanitize_unicode(value).strip() for key, value in zip(fields, row) if key}
            if not values["first_name"] or not values["last_name"]:
                result.issues.append(ParseIssue(line, "incomplete_row"))
                continue
            result.contacts.append(Contact(**values, source="linkedin_import", verified=False))
    except csv.Error:
        result.issues.append(ParseIssue(start + reader.line_num, "malformed_csv"))
    return result
