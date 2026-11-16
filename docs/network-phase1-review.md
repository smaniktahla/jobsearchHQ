# Network Phase 1 review and approval handoff

ATQ 463a1d05; retired Paperclip NIN-143, NIN-147 and NIN-149.

## Scope and findings

The checkout already includes the Phase 1 core, resolver/store, API and Network
page (UI merged by PR #37, commit 5aaf6db). This review uses the extracted issue
text in CONTEXT.md and the wind-down summary. The original SPEC.md and complete
NIN-149 acceptance text are not supplied in this checkout; full spec conformance
has not been established.

Static inspection confirms the directory, employer/search controls, CSV preview
and commit, identity review, notes/relationship editor, export and deletion are
present. Contact values render through textContent. The navigation checks the
config flag; API routes depend on session authentication and a per-user flag.
This is source review, not runtime acceptance evidence.

UI fixes in this change:

- Ignore superseded directory responses so an earlier fetch cannot overwrite a
  newer response.
- Preserve refresh failures after successful mutations, reporting both outcomes
  instead of replacing the error with a success message.
- Reset a removed employer filter to All employers on refresh.
- Capture all editor values before the first request, keeping both writes tied
  to the same submitted contact and values.
- Explain partial saves when notes/verification succeed but the relationship
  request fails. The editor remains available for retry.

No runtime dependencies, real contact data, production changes, or later-phase
features were added.

## Acceptance still required

The dispatcher restricts this run to read-only shell commands and file editing.
Consequently no tests, app start, browser checks, screenshots, or git commit were
performed. A commit and PR URL are still required; the dispatcher owns pushing
and opening the PR. Do not interpret this report as Phase 1 approval.

In an execution-enabled isolated checkout:

1. Run `python -m pytest tests/` with synthetic fixtures. Existing tests cover
   parser/resolver/store behavior, per-user isolation, session-only API access,
   flags off, import limits, edits and relationship preservation.
2. Smoke start with network_enabled off and on using synthetic accounts; verify
   existing pages/endpoints and hidden navigation/404 behavior when off.
3. In a browser, exercise preview without writes, commit, repeat import,
   search/filter, merge/keep separate, edit, export, and both deletion controls.
   Capture synthetic-only screenshots for the PR.
4. Delay an initial directory response, complete a later refresh, then release
   the initial response: the later directory must remain rendered.
5. Fail the export refresh after a successful mutation: the status must report
   the mutation succeeded and the directory refresh failed. Restore service and
   revisit Network to confirm current state.
6. Fail only the relationship PUT: verify the partial-save message and that
   retrying Save persists the relationship. Change editor input during a delayed
   PATCH and confirm the PUT uses the originally submitted relationship value.
7. Compare against the original SPEC.md, attach test/browser evidence to one
   Phase 1 PR, and obtain Salil's review.

## Salil approval boundary

No production deployment or real network import is authorized by this review.
Phases 2–4 remain gated on approval of the preceding phase. The epic remains open
until Phase 1 acceptance and Salil's decision; this run does not implement those
later phases or create replacement tracker tickets.
