from pathlib import Path

import pytest
from pydantic import ValidationError
from network.companies import extract_domain, normalize_company
from network.linkedin_csv import parse_linkedin_bytes, parse_linkedin_csv
from network.models import Company, Contact, ImportBatch, Relationship

FIXTURES = Path(__file__).parent / "fixtures" / "network"


@pytest.mark.parametrize("name", ["connections.csv", "notes.csv"])
def test_exports(name):
    result = parse_linkedin_csv(FIXTURES / name)
    assert not result.issues
    assert len(result.contacts) == 1
    assert result.contacts[0].source == "linkedin_import"
    assert result.contacts[0].verified is False


def test_bom_variants_and_quotes():
    result = parse_linkedin_bytes(b'\xef\xbb\xbfFirstName,LastName,Profile URL,Email,Company Name,Title,Connection Date\r\nSynthetic,Person,,,"Example, Inc.",Engineer,2026-01-01\r\n')
    assert not result.issues
    assert result.contacts[0].company == "Example, Inc."


@pytest.mark.parametrize("data,code", [(b"", "invalid_header"), (b"wrong,header\na,b", "invalid_header"), (b"\xff", "invalid_encoding"), (b'First Name,Last Name\n"broken', "malformed_csv"), (b"First Name,Last Name\nOnly,\n", "incomplete_row"), (b"First Name,Last Name\na,b,c", "malformed_row"), (b"First Name,Last Name,FirstName", "invalid_header")])
def test_invalid(data, code):
    result = parse_linkedin_bytes(data)
    assert result.issues[0].code == code
    assert not result.contacts


def test_limits(tmp_path):
    path = tmp_path / "large.csv"
    path.write_bytes(b"x" * 101)
    assert parse_linkedin_csv(path, max_bytes=100).issues[0].code == "size_limit"
    result = parse_linkedin_bytes(b"First Name,Last Name\na,b\nc,d", max_rows=1)
    assert result.issues[0].code == "row_limit"
    assert not result.contacts
    assert parse_linkedin_csv(tmp_path / "missing").issues[0].code == "unreadable_file"


def test_partial_and_multiline():
    result = parse_linkedin_bytes(b'First Name,Last Name,Company\nGood,Person,"Example\nCompany"\nBad,,Other\nNext,Person,Other')
    assert len(result.contacts) == 2
    assert result.issues[0].row == 4
    assert "Bad" not in repr(result.issues)


@pytest.mark.parametrize("suffix", ["Inc.", "LLC", "Corp", "Ltd", "GmbH", "L.L.C."])
def test_company_suffix(suffix):
    assert normalize_company("  EXAMPLE, " + suffix) == "example"


def test_company_aliases_and_internal_words():
    assert normalize_company("Old Corp", {"old inc": "New LLC"}) == "new"
    assert normalize_company("Inc Research") == "inc research"
    assert normalize_company("ＡＣＭＥ GmbH") == "acme"


@pytest.mark.parametrize("value,expected", [("https://www.Example.com/path", "example.com"), ("example.com/a", "example.com"), ("https://sub.example.com", "sub.example.com"), ("bad host", ""), ("https://user:pass@example.com", ""), ("ftp://example.com", ""), ("https://[broken", "")])
def test_domains(value, expected):
    assert extract_domain(value) == expected


def test_models_and_sanitization():
    from storage import sanitize_unicode
    assert sanitize_unicode("A\ud800B") == "AB"
    records = [Contact(), Company(name="Example"), Relationship(contact_id="synthetic"), ImportBatch()]
    assert len({r.id for r in records}) == 4
    assert all(r.created_at.tzinfo and r.updated_at.tzinfo and not r.verified for r in records)
    with pytest.raises(ValidationError):
        Contact(source="invented")


def test_data_isolation(isolated_data_dir):
    import os
    import storage
    assert storage.DATA_DIR == isolated_data_dir
    assert os.environ["JSHQ_DATA_DIR"] == str(isolated_data_dir)


def test_unicode_parser_sanitizes(monkeypatch):
    # A lone surrogate cannot occur in valid UTF-8; exercise the same sanitizer
    # boundary used by storage for Python strings from future import adapters.
    from network import linkedin_csv
    original = linkedin_csv.sanitize_unicode
    monkeypatch.setattr(linkedin_csv, "sanitize_unicode", lambda value: original(value + "\ud800"))
    result = parse_linkedin_bytes(b"First Name,Last Name\nSynthetic,Person")
    assert result.contacts[0].first_name == "Synthetic"
