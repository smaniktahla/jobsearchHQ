# Network core

This package performs no network requests and activates no application features.
Existing application modules remain unchanged. Network flags and endpoints belong
to later integration work and must default off.

`parse_linkedin_csv(path)` reads a bounded UTF-8 CSV file; use
`parse_linkedin_bytes(data)` for upload previews. Both return `ParseResult` with
contacts, privacy-safe issues (physical row number and stable code), and the count
of nonblank data rows examined. Defaults are 10 MiB and 50,000 rows. Encoding,
header, and limit failures return no contacts. Ordinary malformed/incomplete rows
are skipped. An unrecoverable CSV quoting error stops parsing and reports an
issue; preceding valid rows remain a partial preview. Consumers must show issues
before committing a partial import. Names are required; email and employer are
optional in LinkedIn exports. No identity merging occurs here.

Records have UUIDs, UTC creation/update timestamps, explicit source and verified
state. Import records are unverified. Callers own updating `updated_at` when
persisting edits and keeping records in the correct user's storage.

`normalize_company` strips trailing legal suffixes and applies optional explicit
aliases (one lookup; no inferred corporate relationships). `extract_domain`
returns a URL hostname, including subdomains, without DNS or public-suffix lookup.

Install requirements-dev.txt and run `python -m pytest tests/`. Fixtures contain
synthetic people only; tests isolate JSHQ_DATA_DIR and deny socket connections.
