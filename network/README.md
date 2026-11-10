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

## Resolver and per-user persistence

`NetworkStore(authenticated_user_id)` stores network state beneath the existing
`storage.get_user_dir()` directory. Callers must derive the user ID from the
session, never request input. User IDs reject path separators and traversal.
No API routes or flags are enabled by these modules.

`import_contacts(contacts)` previews counts without creating files; pass
`dry_run=False` to commit. `list_contacts`, `update_contact`, `save_relationship`,
`export`, `delete_contact`, and `delete_all` provide the directory operations.
Export returns detached JSON-compatible state, including provenance and review
items. `contacts.json` contains the contacts map, relationships, review queue,
merge log and authoritative import receipts. `companies.json` and `matches.json`
start as empty reserved collections; `imports/` holds count-only receipts.
Reads cache by nanosecond mtime and size; writes sanitize Unicode and use fsynced
same-directory temporary files followed by atomic replacement. Writers serialize
within one process; multi-process writers need an external lock before use.

Resolution uses normalized LinkedIn profile URL, then email, then full name plus
normalized employer. Conflicting stronger identifiers or multiple candidates go
to review. Name alone only suggests review, never an automatic merge.
`export()['review_queue']` exposes pending items; a human can call
`resolve_review(review_id, target_id)` to merge or omit `target_id` to retain a
separate person. `merge(target_id, source_id)` records both originals and remaps
relationships; `unmerge(merge_id)` restores source identity and relationships,
undoing unchanged merged values while retaining later target edits. Dependent
merges must be reversed in reverse order. Reimport recognizes merged source
identities without overwriting the chosen canonical contact's fields. Imports
preserve saved notes, verification, provenance, and manually entered relationships.
Deletion purges related merge snapshots so deleted people cannot be resurrected.
All matching and persistence are deterministic, with no model dependency.
