# JSHQ Queue, Agent API, Submission Authorization, and Lifecycle Contract Audit

- **Repository audited:** `smaniktahla/jobsearchHQ` (commit `1fe75c9` on `main`)
- **Audit date:** 2026-11-24 (Eastern)
- **Audit mode:** Offline static code audit only; no code modified, no live services called, no runtime state or personal data included.
- **Agreed ownership model:**
  - **JSHQ (single source of truth for jobs):** discovery, listings, descriptions, posting availability, scoring, approval, applicant profile + authoritative answers, tailored résumé + cover letter generation, application eligibility, **submission authorization**, and application lifecycle status.
  - **JobPilot (execution service):** accessibility preflight, browser execution, AI-assisted form completion/recovery, procedural memory, independent verification, and run state/screenshots/logs/submission evidence.

> **Evidence convention:** Every code claim cites `file:line` against the audited commit (`1fe75c9`). **Verified facts** (observed directly in source) are explicitly separated from **inferences / risk assessments** and **proposed designs**.

---

## Executive Summary

| Audit Area | Current Verdict | Highest-Impact Gaps (Verified) | Minimal Remediation Direction |
| :--- | :--- | :--- | :--- |
| **1. `/api/agent/ready-queue` & `Job.ready_to_apply`** | **Does NOT guarantee full contract** | Cover-letter disk files are never checked (`models.py:313`) and `scheduler.py:463-466` never generates cover-letter `.docx` files; no apply-URL check (`models.py:302-321`); `job_search_paused` ignored (`main.py:316-331`); `PATCH /api/agent/jobs/{id}/review` lets an agent self-approve (`main.py:1836-1854`); `auto_approve` sets `review_status=APPROVED` without a human (`auto_approve.py:65`); `ready_to_apply` uses score `>= 5` while `job_liveness` & `archiving` use `tracking_min_score` (default `8`); sort uses model score instead of `effective_total` (`main.py:330`). | Harden `ready_to_apply` (cover-letter `.docx` disk check, HTTP(S) URL check, `applied_at` check, `bullshit_flag`/status guards); enforce `job_search_paused` and `effective_total` sort in `/api/agent/ready-queue`; fix `scheduler.py:463-466` `.docx` generation; restrict `/api/agent/jobs/{id}/review`. |
| **2. `/api/agent/*` Inventory & Missing Capabilities** | **Partial — 5 critical capabilities missing or unsafe** | 19 `/api/agent/*` routes exist (`GET /api/agent/key` is session-only; 13 `/api/agent/jobs/*` routes lack `if not admin_id:` 503 guards). Missing: agent-auth intake with dedup; idempotent `application-result`; `unconfirmed` submission state; closed-posting + preflight evidence ingestion; structured/idempotent job log and single-job read endpoint. | Add `POST /api/agent/intake` (using `storage.find_in_dedup_index`), `POST /api/agent/jobs/{id}/posting-evidence`, `JobStatus.SUBMISSION_UNCONFIRMED`, idempotency keys + transition guards on `application-result`, and extend `AgentEvent`. |
| **3. JSHQ-Owned Submission Authorization** | **Feasible with stdlib only; unsafe on current storage** | `storage.save_job` (`storage.py:65-78`) has no lock and does non-atomic `write_text()`, so two concurrent consumers both succeed (double-submit race) and background threads overwrite concurrent edits. Auth separation between `jshq_session` (`auth.py:355-365`) and `X-API-Key` (`auth.py:215-217`) is clean, though cookies live 30 days (`auth.py:39`) and digest tokens bypass sessions (`auth.py:235-258`). | Add per-job locking (`threading.Lock` + `fcntl.flock`) and temp-file/`fsync`/`os.replace` atomic writes; isolate or version `submission_auth` against stale background `save_job` writes; require human session for grant/revoke and CAS consume + outcome endpoints for `X-API-Key`. |
| **4. Lifecycle Double-Writes & Executor Conflicts** | **Multiple active double-write & regression hazards** | Re-scoring (including `_run_score_all` at `main.py:725,737`) resets `APPLIED`/`INTERVIEW`/`REJECTED` jobs back to `SCORED`; `application-result` has no transition guard and can downgrade `INTERVIEW`/`REJECTED` back to `APPLIED`; background passes (`job_liveness`, `jd_cleanup`, `backfill`, `research_batch`) hold stale job snapshots across I/O and clobber status; `storage.load_job` + `save_job` silently resurrects archived jobs into split-brain duplicates (`storage.py:76-87`); `docx_builder.py` ignores `job_id` in filenames (`docx_builder.py:143-146,453-458`). | Enforce monotonic/validated status transitions in `Job.update_status`; never reset post-apply statuses during scoring; route all job mutations through locked read-modify-write; stop `save_job` from resurrecting archived files; include `job_id` in generated `.docx` filenames. |

---

## 1. Audit of `/api/agent/ready-queue` and `Job.ready_to_apply`

### 1.1 Verified Facts: Definition, Callers, and Everything That Controls `ready_to_apply`

**Computed property definition (`models.py:301-321`):**
`Job.ready_to_apply` is a dynamically computed `@property` (not persisted to JSON). It returns `True` if and only if all of the following hold:
1. `self.effective_total is not None` (`models.py:307`), where `effective_total` returns `score_override.total` if set, else `score.total` (`models.py:229-233`).
2. `self.effective_total >= 5` (`models.py:308`).
3. `self.review_status == ReviewStatus.APPROVED` (`models.py:309`).
4. `not self.ai_skipped` (`models.py:310`), where `ai_skipped` is `self.market_lane == MarketLane.IGNORE and self.score_override is None` (`models.py:240-242`).
5. `self.posting_status != "closed"` (`models.py:311`).
6. `bool(self.tailored_resume_docx)` (`models.py:312`) **and** `Path(self.tailored_resume_docx).exists()` (`models.py:320`).
7. `bool(self.cover_letters)` (`models.py:313`).
8. `self.status not in (JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED, JobStatus.PASSED)` (`models.py:314-317`).

**Endpoint implementation (`main.py:316-331`):**
- `GET /api/agent/ready-queue` validates `X-API-Key` (`main.py:322-324`), resolves `admin_id = get_admin_user_id()` (`main.py:325-327`), loads active jobs via `storage.load_all_jobs(admin_id)` (`main.py:328`, which reads `jobs/` only and excludes `jobs_archive/`, `storage.py:138-145`), filters `[enrich_job(j) for j in jobs if j.ready_to_apply]` (`main.py:329`), and sorts by `(j.get("score") or {}).get("total", 0)` descending (`main.py:330`).
- Every other place that reads `ready_to_apply`: `enrich_job()` (`main.py:104`), `list_jobs()` (`main.py:477-478`), `GET /api/jobs/apply-queue` (`main.py:535-541`), `GET /api/jobs/{job_id}/status` (`main.py:1528`), `GET /api/jobs/{job_id}/application-package` (`main.py:1560`), and `GET /api/stats` (`main.py:2687`).

**Every code path that sets `review_status = ReviewStatus.APPROVED` (the approval gate into `ready_to_apply`):**
1. **Human UI (`PATCH /api/jobs/{job_id}`):** `main.py:1115-1119` — requires browser session cookie (`get_current_user`); logs an `AgentEvent(RESOLVE)` entry.
2. **Email digest one-click link (`POST /api/digest-action`):** `main.py:1070-1086` — verifies a 14-day HMAC token (`auth.py:235-258`) without a session cookie; does **not** log an `AgentEvent`.
3. **Automated approval (`auto_approve.run`):** `auto_approve.py:47-76` — opt-in (`AppConfig.auto_approve_enabled`, default `False`, `models.py:513`), invoked by `scheduler.py:521`, `backfill.py:127`, and `main.py:2298` (on `PUT /api/config`). Sets `job.review_status = ReviewStatus.APPROVED` (`auto_approve.py:65`) for `PENDING` jobs meeting `auto_approve_threshold` (default `9`, `models.py:514`).
4. **Agent API (`PATCH /api/agent/jobs/{job_id}/review`):** `main.py:1836-1854` — authenticated by `X-API-Key`; allows an agent to set `review_status` to `"pending"`, `"approved"`, or `"rejected"` directly, with **no** `AgentEvent` audit entry.

### 1.2 Requirement-by-Requirement Gap Analysis

#### Requirement A: Every returned job is approved (human go/no-go)
- **Verified facts:**
  - `models.py:309` enforces `review_status == ReviewStatus.APPROVED`.
  - `PATCH /api/agent/jobs/{job_id}/review` (`main.py:1836-1854`) accepts `X-API-Key` and permits setting `review_status = "approved"`.
  - `auto_approve.run()` (`auto_approve.py:21-30, 65`) sets `review_status = ReviewStatus.APPROVED` without human action whenever `auto_approve_enabled=True`.
  - `POST /api/digest-action` (`main.py:1070-1086`) sets `review_status = APPROVED` via an unauthenticated bearer URL token valid for 14 days (`auth.py:235`), without checking whether the job has since closed, been applied to, or been archived.
- **Inference / Risk:**
  - `review_status == APPROVED` guarantees only that *some* actor (human UI, digest link, `auto_approve`, or an agent with `X-API-Key`) set the flag. It does **not** guarantee explicit human approval, nor can it serve as a submission-authorization boundary while an agent can self-approve via `PATCH /api/agent/jobs/{job_id}/review`.

#### Requirement B: Tailored résumé AND cover letter exist on disk (not just recorded paths)
- **Verified facts:**
  - **Tailored résumé:** `models.py:312,320` checks `bool(self.tailored_resume_docx)` and `Path(self.tailored_resume_docx).exists()`. It does **not** check `.is_file()` or `st_size > 0`, nor does it verify that the path stays within `storage.get_generated_dir(user_id)`. Separately, `GET /api/jobs/{job_id}/status` (`main.py:1526`) reports `"has_resume": bool(job.tailored_resume_docx)` without `.exists()`, diverging from `enrich_job()` (`main.py:106`).
  - **Cover letter — NO disk check at all:** `models.py:313` checks only `bool(self.cover_letters)`. It never inspects `cl.docx_path` or `Path(cl.docx_path).exists()`. The same omission appears in `enrich_job()` (`main.py:107`), `auto_approve.is_eligible()` (`auto_approve.py:28`), `backfill._has_letters()` (`backfill.py:33-34`), and `GET /api/jobs/{job_id}/status` (`main.py:1527`).
  - **Active production bug in `scheduler.py:463-466`:** `scheduler.run_daily_pipeline()` calls `scoring.generate_cover_letters(job, user_id)` (`scheduler.py:464`) and stores `job.cover_letters = letters` (`scheduler.py:465`), **without calling `docx_builder.generate_cover_letter_docx(...)`**. In `scoring.py:411-415`, `CoverLetter` is instantiated with default `docx_path = ""` (`models.py:95`). By contrast, `main.py:1414-1429`, `main.py:1485-1500`, and `backfill.py:113-122` do render `.docx` files and set `letter.docx_path`.
  - **Package endpoint assumes variants exist:** `GET /api/agent/jobs/{job_id}/package` (`main.py:1623-1627`) unconditionally returns download URLs for `variant=direct` and `variant=brief`, while `GET /api/agent/jobs/{job_id}/cover-letter/download` (`main.py:1746-1748`) returns HTTP `404` if `cl.docx_path` is empty or the file does not exist on disk.
  - **Filename collision bug (`docx_builder.py:143-146, 159, 174, 350, 356, 453-458`):** Both `generate_resume_docx()` and `generate_cover_letter_docx()` accept `job_id` as a parameter and never include it in the output filename (`{author_name} - {title} - {company}.docx` and `{author_name} - Cover Letter ({variant}) - {title} - {company}.docx`).
- **Inference / Risk:**
  - Any job whose cover letters came from the daily scheduled pipeline Step 3 (`scheduler.py:463-466`) is reported as `ready_to_apply == True`, is eligible for `auto_approve`, is skipped by `backfill.find_missing()` (because `_has_letters` returns `True`), and then **404s when JobPilot calls `/api/agent/jobs/{id}/cover-letter/download`**.
  - If two active jobs share the same `(company, title)` (e.g., reposts, multi-location requisitions, or un-deduped manual intake), generating docs for the second job silently overwrites the first job's `.docx` files on disk while both still pass `.exists()`.

#### Requirement C: Job is not closed (and posting liveness is not stale)
- **Verified facts:**
  - `models.py:311` checks `self.posting_status != "closed"` (case-sensitive check against an unconstrained `str`, `models.py:199`).
  - Jobs start with `posting_status = ""` (never checked) and `posting_checked_at = ""` (`models.py:199, 201`). Both `""` and `"unknown"` pass `ready_to_apply`.
  - `job_liveness.due_jobs()` (`job_liveness.py:174-180`) filters by `min_score = storage.load_config(user_id).tracking_min_score` (default `8`, `models.py:508`) and caps each nightly pass at `posting_check_batch` (default `300`, `models.py:510`, `job_liveness.py:36`). Recheck intervals are `RECHECK_OPEN_DAYS = 7` (`job_liveness.py:32`) and `RECHECK_UNKNOWN_DAYS = 14` (`job_liveness.py:33`).
  - `job_liveness.check_job()` (`job_liveness.py:145-154`) checks only `job.url`, never `job.ats_url`.
  - No `/api/agent/*` endpoint exists to mark a posting closed or submit liveness evidence (only session-auth `POST /api/jobs/{job_id}/posting-status`, `main.py:1250-1267`).
- **Inference / Risk:**
  - Approved jobs with `effective_total` of `5–7` are **never** checked by the nightly `job_liveness` sweep (below `tracking_min_score=8`), so closed postings in that band stay in `/api/agent/ready-queue` indefinitely.
  - Even for jobs scoring `>= 8`, a posting marked `"open"` can be up to 7 days stale (or 14 days for `"unknown"`, or never checked when queue size exceeds `posting_check_batch`), and an executor that hits a 404/closed banner on `ats_url` has no sanctioned way to report it back to `posting_status`.

#### Requirement D: Not already applied / actioned (including archived, duplicates, and pause state)
- **Verified facts:**
  - `models.py:314-317` excludes `APPLIED`, `INTERVIEW`, `OFFER`, `REJECTED`, `PASSED`. Only `NEW` and `SCORED` are allowed.
  - `ready_to_apply` does **not** check `self.applied_at` (`models.py:187`). `PATCH /api/jobs/{job_id}` (`main.py:1122-1129`) allows writing `applied_at` without changing `status`.
  - There is **no** in-flight/claimed state and **no** `unconfirmed` submission state in `JobStatus` (`models.py:24-31`).
  - `GET /api/agent/ready-queue` (`main.py:316-331`) does **not** check `AppConfig.job_search_paused` (`models.py:505`), unlike `require_search_active()` (`main.py:124-127`), `scheduler.py:329`, `auto_approve.py:53`, and `rejection_intake.py:173`.
  - **Duplicates in active `jobs/`:** `POST /api/jobs` (`main.py:605-613`) and `POST /api/intake` (`main.py:616-645`) perform **no** dedup check. Where dedup *is* used (`jobspy_search.py:225`, `company_site_search.py:177`, `email_intake.py:178`, `linkedin_intake.py:144,197`), `storage._normalize_url` (`storage.py:221-222`) only strips whitespace, trailing `/`, and lowercases (preserving query strings such as `utm_*`, `trk`, `refId`), and `storage.dedup_entry` (`storage.py:229-235`) does not index `ats_url` or any requisition ID.
  - **Silent archive resurrection (`storage.py:65-87`):** `storage.load_job()` falls back to `jobs_archive/{job_id}.json` when absent from `jobs/` (`storage.py:82-84`), whereas `storage.save_job()` **always** writes to `jobs/{job_id}.json` (`storage.py:76-77`) and marks `archived=False` in `dedup_index.json` (`storage.py:78`) without deleting `jobs_archive/{job_id}.json`. Any endpoint that calls `load_job` then `save_job` on an archived job (e.g., `POST /api/digest-action`, `PATCH /api/agent/jobs/{id}/review`, `PATCH /api/agent/jobs/{id}/ats-url`, `POST /api/agent/jobs/{id}/log`, `PATCH /api/jobs/{id}`) silently moves it back into active `jobs/` and potentially into `/api/agent/ready-queue`.
- **Inference / Risk:**
  - Flipping `job_search_paused = True` does not stop JobPilot from pulling work from `/api/agent/ready-queue`.
  - Duplicate active jobs (same posting via two URLs or manual paste) allow one copy to stay in `ready-queue` after the other is `APPLIED`.
  - An unconfirmed submission that leaves `status = SCORED` stays in `ready-queue` and risks immediate re-submission on the next poll.

#### Requirement E: Has a usable apply URL (and requisition identity)
- **Verified facts:**
  - **`Job.ready_to_apply` (`models.py:302-321`) performs zero checks on `self.url` or `self.ats_url`.**
  - `Job.url` (`models.py:173`) and `Job.ats_url` (`models.py:192`) are plain `str = ""` fields with no scheme/host validation (contrast `AppConfig.network_ai_url`, `models.py:423-436`).
  - `ManualPasteHandler` (`intake.py:35-43`) and `EmailForwardHandler` (`intake.py:129-136`) explicitly create jobs with `url = ""`.
  - `Job` (`models.py:168-203`) has no `requisition_id` / `external_job_id` field and no dedicated `apply_url` validation separating aggregator links from direct ATS portals.
- **Inference / Risk:**
  - Jobs with empty, malformed, `javascript:`/`mailto:`, or `"nan"` URLs are returned as `ready_to_apply == True` and fail at execution time in JobPilot.
  - Without `requisition_id` (or normalized ATS job ID), JSHQ cannot detect cross-board duplicates pointing to the same employer requisition.

#### Requirement F: Eligible (lane filters, score thresholds, deal-breakers, queue ordering, payload shape)
- **Verified facts:**
  - **Hardcoded threshold mismatch:** `ready_to_apply` uses hardcoded `effective_total >= 5` (`models.py:308`). Meanwhile `AppConfig` configures `auto_generate_threshold = 7` (`models.py:511`), `tracking_min_score = 8` (`models.py:508`), and `auto_approve_threshold = 9` (`models.py:514`).
  - **Approved low-band jobs get auto-archived:** `archiving.below_tracking_threshold()` (`archiving.py:57-65`) archives any `NEW`/`SCORED` job with `effective_total < tracking_min_score` after `SCORE_GRACE_DAYS = 3` (`archiving.py:53`), **without checking `review_status == ReviewStatus.APPROVED`**.
  - **Sort-order bug (`main.py:330` and `main.py:540`):** `queue.sort(key=lambda j: (j.get("score") or {}).get("total", 0), reverse=True)` sorts by the **model** score instead of `effective_total`. Because `PUT /api/jobs/{job_id}/score-override` (`main.py:1146-1159`) allows setting a user score override even when `job.score` is `None` or low, user-promoted jobs sort to the bottom of the queue (`0` or old model score).
  - **`bullshit_flag` unchecked:** `ScoreBreakdown.bullshit_flag` (`models.py:59`) is never checked in `ready_to_apply` (`models.py:302-321`) or `auto_approve.is_eligible` (`auto_approve.py:21-30`).
  - **`JobStatus.NEW` allowed:** A `NEW` job with a user `score_override >= 5` (setting an override does not change `status`, `main.py:1154`) and generated docs passes `ready_to_apply`.
  - **No query filters on `/api/agent/ready-queue`:** `main.py:316-331` accepts no query parameters (`lane`, `resume_lane`, `min_score`, `limit`), unlike `GET /api/agent/jobs` (`main.py:334-341`) and `GET /api/jobs` (`main.py:458-466`).
  - **Override + `IGNORE` lane inconsistency:** Overriding a score clears `ai_skipped` (`models.py:240-242`, tested in `tests/test_score_override.py:84-90`), so the job enters `ready-queue` while `job.market_lane` still reads `"ignore"`.
  - **Full payload & deep copy on every poll:** `main.py:328-329` calls `storage.load_all_jobs(admin_id)` (`copy=True` default) and `enrich_job(j)` (including full `raw_jd`, `tailored_resume`, all 3 cover letters, `score_history`, `status_history`, `research`).
  - **Stale `_list_cache` on UI side (`main.py:494-524`):** `GET /api/jobs?ready_to_apply=true` caches JSON keyed on `jobs_signature` (`jobs/*.json` mtime+size) and date; deleting a `.docx` under `generated/` does not invalidate `_list_cache`, causing UI `ready_to_apply` to disagree with `/api/agent/ready-queue`.

### 1.3 Minimal Enforcement Changes in JSHQ

| Priority | File & Symbol | Minimal Change |
| :--- | :--- | :--- |
| **P0** | `scheduler.py:463-466` | Call `docx_builder.generate_cover_letter_docx(...)` and populate `letter.docx_path` for every generated cover letter (matching `backfill.py:113-122` and `main.py:1414-1429`). |
| **P0** | `models.py:301-321` (`Job.ready_to_apply`), `backfill.py:33-34`, `auto_approve.py:28`, `main.py:106-107, 1526-1527` | Define shared helper(s) verifying: (a) `tailored_resume_docx` exists, `.is_file()`, and `st_size > 0`; (b) at least one cover letter (specifically `"direct"` or the required variant) has a non-empty `docx_path` that exists, `.is_file()`, and `st_size > 0`. Reuse across `ready_to_apply`, `auto_approve`, `backfill`, `enrich_job`, and `/status`. |
| **P0** | `models.py:301-321` (`Job.ready_to_apply`) | Require a valid HTTP(S) apply URL (`ats_url` or `url` passing `urlsplit` with `scheme in ("http", "https")` and non-empty `hostname`, rejecting `"nan"`/whitespace); require `not self.applied_at`; exclude new `JobStatus.SUBMISSION_UNCONFIRMED`; normalize `posting_status.lower() != "closed"`; require `not (self.score and self.score.bullshit_flag and self.score_override is None)`. |
| **P0** | `main.py:316-331` (`agent_ready_queue`) | Check `config.job_search_paused` — return `{"count": 0, "paused": True, "jobs": []}` (or HTTP `423`) when paused; sort by `(j.get("effective_total") or 0)` desc (also fix `main.py:540`); use `storage.load_all_jobs(admin_id, copy=False)`; support optional `lane`, `resume_lane`, `min_score`, `limit`, `slim` query params. |
| **P0** | `main.py:1836-1854` (`agent_set_review_status`) | Forbid setting `review_status = "approved"` via `X-API-Key` (return `403`); log an `AgentEvent` on any allowed transition (or deprecate the endpoint once posting-evidence exists). |
| **P1** | `job_liveness.py:157-177`, `archiving.py:57-65` | Always include `review_status == ReviewStatus.APPROVED` jobs in `job_liveness.due_jobs()` regardless of `tracking_min_score`; exempt `review_status == ReviewStatus.APPROVED` jobs from `archiving.below_tracking_threshold()`. |
| **P1** | `docx_builder.py:143-146, 453-458` | Include `job_id` in generated résumé and cover-letter filenames (or place them under `generated/{job_id}/`) so duplicate `(company, title)` jobs cannot overwrite each other's files. |
| **P1** | `storage.py:65-87` | Prevent `save_job()` from silently resurrecting archived jobs (track archive state explicitly or require `unarchive_job()` before writing to `jobs/`, and unlink `jobs_archive/{job_id}.json` whenever unarchived). |
| **P2** | `models.py:168-203`, `storage.py:221-235` | Add optional `requisition_id: str = ""`, canonicalize query tracking params in `_normalize_url`, index `ats_url` in `dedup_index.json`, and run dedup on `POST /api/intake`. |

---

## 2. Inventory of `/api/agent/*` Endpoints and Missing Execution-Service Capabilities

### 2.1 Complete Verified Inventory of Agent-Scoped Endpoints

All 19 routes under `/api/agent/*` plus 1 additional route outside `/api/agent/*` that authenticates with `X-API-Key`:

| # | Method & Path | Code Location | Auth Mechanism | `admin_id` Null Guard? | Mutates State? | Verified Behavior & Contract Notes |
| :-: | :--- | :--- | :--- | :-: | :-: | :--- |
| 1 | `GET /api/agent/ready-queue` | `main.py:316-331` | `X-API-Key` | Yes (`503`) | No | Active jobs where `j.ready_to_apply`; full `enrich_job` payload; sorts by model `score.total` (bug); ignores `job_search_paused`; no query filters. |
| 2 | `GET /api/agent/jobs` | `main.py:334-368` | `X-API-Key` | Yes (`503`) | No | Active jobs only (`load_all_jobs`); filters `status`, `company`, `q`, `lane`; full payload; sorted by `updated_at` desc. No single-job `GET /api/agent/jobs/{id}` exists. |
| 3 | `GET /api/agent/profile` | `main.py:371-398` | `X-API-Key` | Yes (`503`) | No | Returns applicant profile fields from `AppConfig`. No screening-answers / authoritative Q&A endpoint exists yet. |
| 4 | `PUT /api/agent/profile` | `main.py:401-428` | `X-API-Key` | Yes (`503`) | Yes (`config.json`) | Updates applicant profile fields on `AppConfig`. |
| 5 | `GET /api/agent/writing-samples` | `main.py:431-442` | `X-API-Key` | Yes (`503`) | No | Returns writing-sample metadata, text, and prompt block (empty when disabled). |
| 6 | `GET /api/agent/key` | `main.py:445-453` | **Session cookie + `is_admin`** | N/A | No | **Not** agent-key auth; admin browser endpoint to read `agent_api_key`. |
| 7 | `GET /api/agent/jobs/{job_id}/package` | `main.py:1597-1628` | `X-API-Key` | **No** (500 if None) | No | Loads via `load_job` (includes archived!); returns subset of job fields + hardcoded resume/direct/brief download URLs without verifying files exist or job is eligible. |
| 8 | `GET /api/agent/jobs/{job_id}/research` | `main.py:1631-1648` | `X-API-Key` | **No** | No | Read-only return of `job.research`. |
| 9 | `POST /api/agent/jobs/{job_id}/research` | `main.py:1651-1685` | `X-API-Key` | **No** | Yes (paid API + `save_job`) | Triggers live company research and persists `job.research`. |
| 10 | `GET /api/agent/jobs/{job_id}/resume` | `main.py:1698-1714` | `X-API-Key` | **No** | No | Returns resolved resume lane, `available: bool` (checks `.docx` exists on disk), and download URL. |
| 11 | `GET /api/agent/jobs/{job_id}/resume/download` | `main.py:1717-1733` | `X-API-Key` | **No** | No | Streams tailored résumé `.docx`; `404` if missing on disk. |
| 12 | `GET /api/agent/jobs/{job_id}/cover-letter/download` | `main.py:1736-1749` | `X-API-Key` | **No** | No | Streams cover letter `.docx` for `?variant=direct` (default); `404` if variant missing or `docx_path` empty/missing. Note `request: Request = None` signature (`main.py:1737`). |
| 13 | `PATCH /api/agent/jobs/{job_id}/ats-url` | `main.py:1752-1765` | `X-API-Key` | **No** | Yes (`save_job`) | Writes `job.ats_url`; no URL validation, no `agent_log` entry, resurrects archived jobs on save. |
| 14 | `POST /api/agent/jobs/{job_id}/log` | `main.py:1768-1781` | `X-API-Key` | **No** | Yes (`save_job`) | Appends `AgentEvent` to `job.agent_log`; non-idempotent; rewrites whole job + `dedup_index.json`; resurrects archived jobs. |
| 15 | `GET /api/agent/jobs/{job_id}/log` | `main.py:1784-1805` | `X-API-Key` | **No** | No | Returns `job.agent_log` events + Claude token totals. |
| 16 | `POST /api/agent/jobs/{job_id}/application-result` | `main.py:1809-1833` | `X-API-Key` | **No** | Yes (`save_job`) | Non-idempotent; no transition guard; no unconfirmed state; no `agent_log` entry; resurrects archived jobs. |
| 17 | `PATCH /api/agent/jobs/{job_id}/review` | `main.py:1836-1854` | `X-API-Key` | **No** | Yes (`save_job`) | Lets agent set `review_status` to `pending`/`approved`/`rejected` with no audit log. **Over-privileged.** |
| 18 | `GET /api/agent/jobs/needs-refresh` | `main.py:1857-1881` | `X-API-Key` | **No** | No | Lists active `PENDING` unscored jobs with `len(raw_jd) < 200` and non-empty `url`. |
| 19 | `POST /api/agent/jobs/{job_id}/refresh-and-score` | `main.py:1891-1926` | `X-API-Key` | **No** | Yes (LLM + `save_job`) | Replaces `raw_jd`, scores via LLM, and unconditionally sets `status = SCORED` (even on `APPLIED` jobs!). |
| 20* | `POST /api/jobs/send-scored-digest` | `main.py:908-1022` | `X-API-Key` only* | **No** | Sends email | Outside `/api/agent/*`. Docstring (`main.py:910`) says "agent API key or user session", but `main.py:911-915` rejects session cookies and accepts only `X-API-Key`. |

> **Verified defect across routes #7–#20:** All 14 handlers call `admin_id = get_admin_user_id()` without checking `if not admin_id: raise HTTPException(503, ...)`. If called before setup completes, `storage.get_user_dir(None)` (`storage.py:36`) raises `TypeError` (HTTP `500`).

### 2.2 Capability-by-Capability Assessment for an Execution Service

#### 1. Agent-Auth Job Intake & Deduplication (including archived fingerprints)
- **Verified facts:**
  - All three intake/creation routes require a browser session cookie (`Depends(get_current_user)`): `POST /api/jobs` (`main.py:605-613`), `POST /api/intake` (`main.py:616-645`), and `POST /api/intake/linkedin-bulk` (`main.py:648-656`). None accept `X-API-Key`.
  - `POST /api/jobs` and `POST /api/intake` do **not** call `storage.find_in_dedup_index` or check `job_search_paused`.
  - `POST /api/intake/linkedin-bulk` delegates to `linkedin_intake.process_linkedin_capture()` (`linkedin_intake.py:117-244`), which **does** dedupe via `storage.load_dedup_index(user_id)` and `storage.find_in_dedup_index(...)` (`linkedin_intake.py:113-114, 125, 144, 197`). However:
    - `linkedin_intake.py:56, 81` hardcode `anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))` with `claude-sonnet-4-20250514`, bypassing `ai_router`.
    - `linkedin_intake.py:157` (and `jobspy_search.py:310`) have a dead ternary: `MarketLane.CONTRACT if ... else MarketLane.CONTRACT`.
  - `storage.find_in_dedup_index()` (`storage.py:260-271`) matches against `dedup_index.json`, which preserves entries for archived jobs with `"archived": True` (`storage.py:171-183, 229-235`).
  - **Dedup index limitations (verified):**
    1. `storage.delete_job()` (`storage.py:200-206`) never removes the deleted job's entry from `dedup_index.json` (permanently blocking re-intake of a deleted URL/title+company until `migrate_archive.py` rebuilds the index).
    2. `_normalize_url()` (`storage.py:221-222`) only strips whitespace, trailing `/`, and lowercases; query-string tracking parameters break URL matching.
    3. `dedup_entry()` (`storage.py:229-235`) indexes `job.url` and `title|company` only — not `job.ats_url` or requisition IDs.
    4. `update_dedup_index()` (`storage.py:253-257`) is an unlocked read-modify-write of the entire index file.
- **Gap & recommendation:**
  - **Missing:** `POST /api/agent/intake` (and optionally `POST /api/agent/intake/batch`) authenticated via `X-API-Key`.
  - **Minimum contract:** Honor `require_search_active(admin_id)` (`423` when paused); accept structured fields (`title`, `company`, `url`, `ats_url`, `raw_jd`, `source`, `pay_range`, `requisition_id`, `auto_score: bool = False`); dedupe against `storage.find_in_dedup_index` (matching `url`, `ats_url`, and normalized `title|company`, including `archived: true` fingerprints) under lock; return `{created: false, duplicate_of: "<id>", archived: true|false}` (HTTP `200` or `409`) instead of creating duplicates.

#### 2. Idempotent Confirmed-Application Writeback (`POST /api/agent/jobs/{id}/application-result`)
- **Verified facts (`main.py:1809-1833`, `models.py:204-225, 329-335`):**
  - **Is it idempotent? No.**
  - **On a repeat call with the same payload:**
    1. `job.update_status(result.status, applied_date=result.applied_at)` (`main.py:1821`) **unconditionally appends** another `{"from": ..., "to": ..., "at": ...}` entry to `job.status_history` (`models.py:205-209`) and updates `job.updated_at` (`models.py:211`).
    2. `job.applied_at` and `job.follow_up` are preserved only because `models.py:221` checks `if not self.applied_at:`.
    3. `job.notes` (`main.py:1822-1823`), `job.follow_up.notes` (`main.py:1824-1828`), and `Automation error:` (`main.py:1829-1830`) are **unconditionally concatenated**, duplicating text on every retry.
    4. No `idempotency_key`, `run_id`, or `confirmation_id` field exists on `ApplicationResult` (`models.py:329-335`).
    5. No `AgentEvent` is appended to `job.agent_log`.
  - **When `job.status` is already `APPLIED` (or later: `INTERVIEW`, `OFFER`, `REJECTED`):**
    1. `Job.update_status()` (`models.py:204-225`) performs **no transition validation**.
    2. If `job.status` is already `APPLIED`, a repeat call appends a `applied -> applied` history row and duplicates `notes`/`follow_up.notes`.
    3. If `job.status` has already advanced to `INTERVIEW`, `OFFER`, or `REJECTED` (e.g., via human update or `rejection_intake.py:211`), a delayed/retried call with default `status=JobStatus.APPLIED` (`models.py:331`) **silently downgrades `job.status` back to `APPLIED`**!
    4. If a second call sends `status=PASSED` or `status=SCORED`, it overwrites `APPLIED` while leaving `applied_at` and `follow_up.due_at` set.
  - **On an unconfirmed submission:**
    1. `ApplicationResult.status` defaults to `JobStatus.APPLIED` (`models.py:331`), with no `confirmed: bool` or verification-evidence schema.
    2. Reporting `status="applied"` falsely triggers `applied_at`, schedules a 14-day follow-up (`models.py:222-223`), and exposes the job to `rejection_intake.py:176-177`.
    3. Reporting `status="scored"` leaves `ready_to_apply == True`, causing the next queue poll to re-dispatch the job and risk a duplicate application.
- **Gap & recommendation:**
  - Require an `idempotency_key` (e.g., `{run_id}:{attempt_id}` or `Idempotency-Key` header) stored on the job/authorization record; treat repeat calls with the same key as a `200 OK` no-op returning current state.
  - Enforce monotonic lifecycle transition guards (never downgrade `INTERVIEW`/`OFFER`/`REJECTED` to `APPLIED`; return `409 Conflict` on conflicting status transitions).
  - Require `auth_id` (from Section 3) + structured verification evidence (`confirmation_level`, `confirmation_id`, `evidence_ref`) before accepting a transition to `APPLIED`, and atomically append a single `AgentEvent` inside the same locked write.

#### 3. Separate "Unconfirmed Submission" State
- **Verified facts:**
  - `JobStatus` (`models.py:24-31`) has exactly 7 values: `new`, `scored`, `applied`, `interview`, `offer`, `rejected`, `passed`. There is no unconfirmed-submission state.
- **Inference / Risk:**
  - Under the agreed ownership model, JobPilot performs independent post-submit verification. When Submit was clicked (or timed out in flight) and verification cannot prove receipt, conflating that outcome with `APPLIED` corrupts JSHQ's source of truth, while leaving it `SCORED` leaves `ready_to_apply == True` and risks double-submitting to the employer.
- **Recommendation:**
  - Add `JobStatus.SUBMISSION_UNCONFIRMED = "submission_unconfirmed"` to `models.py:24-31`.
  - Semantics across JSHQ:
    - Excluded from `Job.ready_to_apply` (`models.py:314-317`) and `auto_approve._CLOSED` (`auto_approve.py:17`).
    - Excluded from `job_liveness.SKIP_STATUSES` (`job_liveness.py:29`), `backfill._CLOSED` (`backfill.py:22`), and `research_batch._CLOSED` (`research_batch.py:19`).
    - Added to `archiving.ACTIVE_STATUSES` (`archiving.py:25`) so unconfirmed submissions are **never** silently auto-archived before human review.
    - Excluded from `follow_up_due` (`models.py:281`) and `rejection_intake` (`rejection_intake.py:177`) until a human confirms receipt and promotes to `APPLIED` (or verifies non-submission and explicitly grants a new submission authorization).
    - Does **not** set `applied_at` or `follow_up.due_at`, but records `submission_attempt_at`, `evidence_ref`, and reason.

#### 4. Accepting Closed-Posting + Accessibility-Preflight Evidence Without a Competing Job Status Field
- **Verified facts:**
  - JSHQ **already** separates posting availability from workflow status (`models.py:198-202`, `job_liveness.py:4-9`):
    - `posting_status: str = ""` (`"" | "open" | "closed" | "unknown"`), `posting_reason`, `posting_checked_at`, `posting_closed_at`.
    - `job_liveness.py:4-9` explicitly requires hard signals for `"closed"` (`404`/`410`, Greenhouse `?error=true`, expired JSON-LD `validThrough`, `CLOSED_PHRASES`), positive signals for `"open"`, and maps login walls/blocks/ambiguous responses to `"unknown"` (`job_liveness.py:105-131`).
    - Manual UI overrides prefix `posting_reason` with `MANUAL_REASON = "set manually"` (`job_liveness.py:31, 160`; `main.py:1262`) so automated checks do not overwrite human judgment.
    - `ready_to_apply` (`models.py:311`), `auto_approve` (`auto_approve.py:26`), and `archiving.closed_posting()` (`archiving.py:74-78`) all consume `posting_status` directly.
  - **Missing:** There is **no** `/api/agent/*` endpoint to report either closed-posting evidence or accessibility-preflight findings (CAPTCHA, login/account wall, unsupported ATS, broken form).
- **Inference / Risk:**
  - Without a dedicated evidence endpoint, an execution service is forced either to maintain a competing status store in JobPilot (violating JSHQ single-source-of-truth) or to abuse `PATCH /api/agent/jobs/{id}/review` (`rejected`) / `application-result` (`passed`), corrupting human review/application semantics.
- **Recommended design (zero competing workflow status fields):**
  - Add `POST /api/agent/jobs/{job_id}/posting-evidence` (`X-API-Key`):
    - **Closed-posting evidence (`kind = "closed"`):** requires concrete signal (`http_status`, `matched_phrase`, `final_url`, `evidence_ref`). Writes directly to JSHQ's existing `posting_status = "closed"`, `posting_reason = "executor: <signal>"`, `posting_checked_at`, and `posting_closed_at` — **without touching `Job.status` or `review_status`**, and refusing to override a `MANUAL_REASON` `"open"` override unless `force` is not allowed (return `409`).
    - **Accessibility-preflight evidence (`kind = "preflight"`):** stores a non-workflow metadata block on `Job` (e.g., `preflight: Optional[PreflightEvidence]` with `checked_at`, `accessible: bool`, `blocker: "none" | "captcha" | "login_wall" | "account_required" | "unsupported_ats" | "form_error" | "network"`, `ats_vendor`, `final_url`, `evidence_ref`).
      - Never creates a second job lifecycle status (`Job.status` stays `SCORED`).
      - Never falsely marks `posting_status = "closed"` on a CAPTCHA or login wall (consistent with `job_liveness.py:125-126` mapping login walls to `"unknown"`).
      - When preflight positively confirms an open form (`accessible=True`) and `posting_reason` is not `MANUAL_REASON`, it may refresh `posting_status = "open"` and `posting_checked_at`, keeping liveness fresh.
      - Optionally updates `job.ats_url` when `final_url` is a validated direct ATS URL, and appends an `AgentEvent` in the same atomic write.

#### 5. The Job Log Endpoint (`POST` / `GET /api/agent/jobs/{id}/log`)
- **Verified facts (`main.py:1768-1805`, `models.py:147-166`):**
  - `POST /api/agent/jobs/{job_id}/log` and `GET /api/agent/jobs/{job_id}/log` already exist under `X-API-Key`.
  - **Observed gaps:**
    1. `AgentEventType` (`models.py:147-153`) supports only `score | dispatch | escalate | resolve | submit | error` — missing `preflight`, `auth_grant`, `auth_consume`, `auth_revoke`, `unconfirmed`, `verify`, `intake`.
    2. `AgentEvent` (`models.py:156-165`) lacks `idempotency_key`, `run_id`, `actor`, and `evidence_ref`, and has provider-specific fields (`claude_tokens_in`/`claude_tokens_out`).
    3. Repeat `POST` calls append duplicate events (no dedup on `event.id` or `idempotency_key`).
    4. Each append rewrites the entire `{job_id}.json` + `dedup_index.json` without locking (`main.py:1780`, `storage.py:77-78`) and silently resurrects archived jobs (`storage.py:76-87`).
    5. Missing `if not admin_id:` 503 guard (`main.py:1774, 1790`).
- **Additional minor agent-API gap:** There is no `GET /api/agent/jobs/{job_id}` single-job read endpoint (agents must either list all active jobs via `GET /api/agent/jobs` or accept the stripped `/package` view, which omits `status`, `review_status`, `posting_status`, `applied_at`, and `submission_auth`).

---

## 3. Feasibility & Design of JSHQ-Owned Submission Authorization

### 3.1 Verified Baseline: Storage, Concurrency, and Authentication

**Runtime & concurrency (`Dockerfile:10`, `main.py:2882-2884`, `activity.py:8`):**
- JSHQ runs a single `uvicorn` process (`uvicorn.run(app, host="0.0.0.0", port=port)`), as noted in `activity.py:8`.
- However, **nearly all HTTP route handlers are synchronous `def` functions** (including `main.py:317, 1101, 1567, 1753, 1769, 1810, 1837, 1892`), which FastAPI executes concurrently across Starlette's AnyIO worker thread pool.
- In addition, `APScheduler` (`BackgroundScheduler`, `scheduler.py:94-98`) and ad-hoc daemon threads (`main.py:756, 817, 1286, 2298`, `backfill.py:158`, `research_batch.py:158`) read and write job files concurrently, and CLI maintenance scripts (`migrate_archive.py`) can run in a separate process via `docker exec`.

**How jobs are saved (`storage.py:65-88`):**
- `save_job()` writes directly via `path.write_text(json.dumps(data, indent=2))` (`storage.py:77`) — **no lock, no temporary file, no `fsync`, no `os.replace()`** (unlike `local_users.py:31, 91-97` and `network/store.py:22, 50-60`, which already use `RLock` + atomic temp-file replacement).
- `_jobs_cache_lock` (`storage.py:95`) protects only the in-memory `_jobs_cache` dict during reads (`storage.py:118`), **not** disk read-modify-write sequences.
- Every `save_job()` also performs an unlocked read-modify-write of `dedup_index.json` (`storage.py:78, 253-257`).

**Concurrent consumer race on current storage (verified mechanism -> inferred failure):**
If two executor workers (or a retried request and an active worker) call a naive `consume` endpoint simultaneously:
1. Worker A calls `storage.load_job(admin_id, job_id)` -> observes `state == "granted"`.
2. Worker B calls `storage.load_job(admin_id, job_id)` -> also observes `state == "granted"`.
3. Worker A sets `state = "consumed"` and calls `save_job()` (`write_text`, truncating the file in place).
4. Worker B sets `state = "consumed"` and calls `save_job()` (overwriting Worker A's file, or interleaving bytes and corrupting JSON).
5. **Both workers receive `200 OK` and proceed to submit the same application twice.**
6. Independently, any background writer (`job_liveness.py:206-212`, `jd_cleanup.py:69-72`, `backfill.py:105-123`, `research_batch.py:122-123`) that loaded `Job` *before* grant or consume will **overwrite the entire job JSON** upon completion, **re-arming a consumed authorization or erasing a revocation!**

**Authentication separation (`auth.py`):**
- **Human session (`auth.py:38-39, 172-179, 278-305, 355-365`):** `get_current_user` requires the `jshq_session` HTTP-only HMAC-SHA256 cookie signed with `session_secret`, issued only via local password auth (`auth.py:377-388`), OIDC callback (`auth.py:410-475`), or first-run setup (`main.py:204-210`).
- **Agent API key (`auth.py:204-217`):** `agent_api_key` is a distinct 32-byte hex secret checked via `hmac.compare_digest` in `verify_agent_api_key()`. It is **never** accepted by `get_current_user`.
- **Digest action token (`auth.py:225-258`):** Uses a third secret (`digest_action_secret`) with 14-day TTL for email links; must **not** be accepted for submission authorization.
- **Security caveats (verified):**
  1. All three secrets (`session_secret`, `agent_api_key`, `digest_action_secret`) are stored in `/app/data/system_config.json` (`auth.py:126-130, 172-233`). Keeping JobPilot isolated from JSHQ's `/app/data` volume is therefore a mandatory trust boundary.
  2. `jshq_session` cookies have `COOKIE_MAX_AGE = 86400 * 30` (30 days, `auth.py:39`) and `samesite="lax"` (`auth.py:288`), with `secure` controlled by `X-Forwarded-Proto: https` (`auth.py:87-88`).

### 3.2 Feasibility Assessment

**Conclusion: Fully feasible in JSHQ with zero new third-party dependencies (Python 3.11 stdlib only).**

To guarantee atomic single-use consumption AND prevent stale background writers from clobbering authorization state, two storage primitives are needed:
1. **Per-job atomic storage locking (both in-process and cross-process):**
   - Acquire an in-process per-`(user_id, job_id)` `threading.Lock` (from a lock registry) **plus** an advisory OS lock via `fcntl.flock(LOCK_EX)` on `{user_dir}/.locks/{job_id}.lock` (available on Linux / `python:3.11-slim`, `Dockerfile:1`).
   - Persist JSON via temp-file in the same directory + `flush()` + `os.fsync()` + `os.replace()` (identical to `network/store.py:50-60` and `local_users.py:91-97`).
   - Also wrap `update_dedup_index()` (`storage.py:253-257`) with a per-user lock + atomic replace, and skip rewriting `dedup_index.json` when `(url, title, company, archived)` did not change.
2. **Isolation from legacy whole-job overwrites (critical design choice):**
   - **Recommended:** Store authoritative submission-authorization records in a dedicated per-user SQLite WAL database (`{user_dir}/submission_auth.db` via Python stdlib `sqlite3`, with `BEGIN IMMEDIATE` transactions and `UNIQUE`/`CHECK` constraints) **or** a dedicated per-job file `{user_dir}/submission_auth/{job_id}.json` written exclusively by the authorization module, and project the current summary read-only onto `enrich_job(job)` (never accepting `submission_auth` from generic `save_job(user_id, job)`).
   - **Why this matters:** Even if a legacy code path (`job_liveness`, `jd_cleanup`, `backfill`, `research_batch`, `score_all`) calls `load_job` -> slow I/O -> `save_job`, it **physically cannot** revert a `consumed` or `revoked` authorization back to `granted`. Using stdlib `sqlite3` in WAL mode additionally gives ACID compare-and-swap (`UPDATE ... WHERE job_id=? AND auth_id=? AND version=? AND state='granted' AND expires_at > ?`), append-only audit rows, and safe cross-process concurrency out of the box without adding any dependency to `requirements.txt`.
   - If keeping the attribute embedded directly in `{job_id}.json` is preferred for single-file simplicity, then `storage.save_job()` MUST acquire the per-job lock, preserve the on-disk `submission_auth` and `submission_auth_history` fields for non-auth callers, and enforce optimistic version checks.

### 3.3 Proposed Data Model Change (`models.py`)

Keep `review_status` (`PENDING | APPROVED | REJECTED`, `models.py:141-144`) strictly as the **queue-eligibility / shortlist decision**. Add an orthogonal **submission authorization** model:

```python
class SubmissionAuthState(str, Enum):
    GRANTED  = "granted"   # Armed, unexpired, unused
    CONSUMED = "consumed"  # Single-use token atomically claimed by one run
    REVOKED  = "revoked"   # Cancelled by human before consumption
    EXPIRED  = "expired"   # TTL elapsed before consumption


class SubmissionOutcome(str, Enum):
    CONFIRMED_SUBMITTED    = "confirmed_submitted"
    UNCONFIRMED_SUBMISSION = "unconfirmed_submission"
    ABORTED_BEFORE_SUBMIT  = "aborted_before_submit"
    FAILED_BEFORE_SUBMIT   = "failed_before_submit"


class SubmissionAuthScope(BaseModel):
    job_id: str
    target_url: str           # Validated ats_url or url pinned at grant time
    resume_sha256: str        # SHA-256 of tailored_resume_docx bytes at grant time
    cover_letter_variant: str = "direct"
    cover_letter_sha256: str = ""  # SHA-256 of cover letter .docx bytes at grant time


class SubmissionAuthorization(BaseModel):
    auth_id: str              # secrets.token_urlsafe(16) — unique per grant
    state: SubmissionAuthState = SubmissionAuthState.GRANTED
    version: int = 1          # Monotonic CAS counter
    granted_by: str           # Human user.id from verified session
    granted_at: str           # UTC ISO-8601
    expires_at: str           # UTC ISO-8601 (bounded TTL, e.g. default 3600s, max 86400s)
    scope: SubmissionAuthScope
    note: str = ""
    # Revocation metadata
    revoked_at: str = ""
    revoked_by: str = ""
    revoke_reason: str = ""
    # Consumption metadata (single-use)
    consumed_at: str = ""
    consumed_by_run_id: str = ""
    consumed_by_agent: str = ""
    consume_idempotency_key: str = ""
    # Execution outcome metadata
    outcome: Optional[SubmissionOutcome] = None
    outcome_at: str = ""
    outcome_idempotency_key: str = ""
    confirmation_id: str = ""
    evidence_ref: str = ""
    outcome_notes: str = ""


class SubmissionAuthAuditEntry(BaseModel):
    id: str
    at: str                   # UTC ISO-8601
    auth_id: str
    event: str                # granted | revoked | expired | consumed | consume_rejected | outcome_reported
    actor_type: str           # human_session | agent_key | system
    actor_id: str             # user.id or agent/run id
    run_id: str = ""
    reason: str = ""
    client_ip: str = ""
```

**On `Job` (`models.py:168-203`):**
- Add read-only/projected `submission_auth: Optional[SubmissionAuthorization] = None` (and `submission_auth_history` exposed via the auth/detail endpoint rather than bloated into list responses).
- Add `JobStatus.SUBMISSION_UNCONFIRMED = "submission_unconfirmed"` (`models.py:24-31`).

### 3.4 Human-Held Secret / Session Requirement

1. **Grant & Revoke require `Depends(get_current_user)` + `require_admin` (or job owner) ONLY:**
   - Explicitly reject any request carrying `X-API-Key` (even if a session cookie were somehow absent) and never accept `digest_action` tokens (`auth.py:235-258`).
   - Because `jshq_session` is an HTTP-only cookie signed with `session_secret` (`auth.py:278-290`) and issued only after local password verification or OIDC SSO login, an external executor holding only `agent_api_key` cannot mint or satisfy this credential.
2. **CSRF defense on Grant/Revoke:** Require `Content-Type: application/json` plus a custom header (e.g., `X-JSHQ-Intent: submission-auth`) so cross-site form posts cannot trigger a grant even with `SameSite=Lax`.
3. **Optional step-up assurance (recommended if unattended 30-day cookies are a concern):**
   - Because `COOKIE_MAX_AGE` is 30 days (`auth.py:39`), an operator can optionally require a **human-held step-up secret** for grants — either (a) recent authentication (`auth_time` claim embedded in `jshq_session` within N minutes), (b) local password / dedicated human-only submission passphrase/PIN (stored scrypt-hashed in `users.json` or an operator-only env/hardware-backed secret never exposed to `/api/agent/*`), or (c) an explicit per-job confirmation challenge echoing `(job_id, company, resume_sha256[:8])`.
   - Importantly, `auto_approve.run()` (`auto_approve.py:47-76`) must **NEVER** call the grant function; it may at most set `review_status = APPROVED` (queue eligibility), never `submission_auth`.

### 3.5 Proposed Endpoint Shapes

#### A. Human-Session Only Endpoints (`Depends(get_current_user)`, rejects `X-API-Key`)

1. **Grant single-use authorization:** `POST /api/jobs/{job_id}/submission-auth`
   - **Preconditions enforced server-side under lock:**
     - Job exists in active `jobs/` (not archived).
     - Job satisfies `ready_to_apply` (including disk-verified résumé + cover letter `.docx`, valid HTTP(S) `ats_url or url`, `posting_status != "closed"`, `status in (NEW, SCORED)`, `not applied_at`, `not job_search_paused`).
     - No active unexpired `GRANTED` authorization already exists (returns `409 Conflict` unless caller explicitly passes `replace_existing: true`, which atomically revokes the prior grant and logs it).
     - Computes `resume_sha256` and `cover_letter_sha256` from disk files at grant time and pins `target_url = job.ats_url or job.url`.
   - **Request body:**
     ```json
     {
       "ttl_seconds": 3600,
       "cover_letter_variant": "direct",
       "note": "Verified comp range and role details",
       "step_up_secret": "(optional if step-up enabled)"
     }
     ```
   - **Response (`201 Created`):**
     ```json
     {
       "job_id": "a1b2c3d4",
       "auth_id": "subauth_9f8e7d6c5b4a3210",
       "state": "granted",
       "version": 1,
       "granted_by": "u_admin",
       "granted_at": "2026-11-24T22:00:00+00:00",
       "expires_at": "2026-11-24T23:00:00+00:00",
       "scope": {
         "job_id": "a1b2c3d4",
         "target_url": "https://boards.example.com/jobs/12345",
         "resume_sha256": "...",
         "cover_letter_variant": "direct",
         "cover_letter_sha256": "..."
       }
     }
     ```
   - **Error codes:** `401`/`403` (missing session or `X-API-Key` attempted), `404` (job missing/archived), `409` (not eligible, already applied/unconfirmed, or active grant exists), `422` (TTL out of bounds, e.g., `< 60s` or `> 86400s`), `423` (`job_search_paused`).

2. **Revoke authorization:** `POST /api/jobs/{job_id}/submission-auth/revoke` (or `DELETE /api/jobs/{job_id}/submission-auth`)
   - **Request body:** `{"auth_id": "subauth_...", "reason": "Changed mind"}` (`auth_id` optional; revokes current `granted` auth if omitted).
   - **Behavior under lock:**
     - If `state == "granted"` -> atomically transitions to `revoked`, increments `version`, appends `revoked` audit entry -> `200 OK`.
     - If already `revoked` or `expired` -> idempotent `200 OK` returning current state.
     - If already `consumed` -> returns `409 Conflict` with `{state: "consumed", consumed_at, consumed_by_run_id}` (cannot un-consume).

3. **Inspect authorization & audit trail:** `GET /api/jobs/{job_id}/submission-auth`
   - Returns `{current: SubmissionAuthorization | null, history: [SubmissionAuthAuditEntry, ...]}`.

#### B. Agent-Key Endpoints (`X-API-Key` + `admin_id` guard; can NEVER grant or revoke)

4. **Read current authorization:** `GET /api/agent/jobs/{job_id}/submission-auth`
   - Lazily evaluates `now >= expires_at` (reporting `state: "expired"` if elapsed).
   - Returns:
     ```json
     {
       "job_id": "a1b2c3d4",
       "authorized": true,
       "can_consume": true,
       "auth_id": "subauth_9f8e7d6c5b4a3210",
       "state": "granted",
       "version": 1,
       "granted_at": "2026-11-24T22:00:00+00:00",
       "expires_at": "2026-11-24T23:00:00+00:00",
       "remaining_seconds": 3420,
       "scope": {
         "target_url": "https://boards.example.com/jobs/12345",
         "resume_sha256": "...",
         "cover_letter_variant": "direct",
         "cover_letter_sha256": "..."
       }
     }
     ```
   - `GET /api/agent/ready-queue` can also include a lightweight boolean `has_submission_auth: bool` and `submission_auth_expires_at` per job (or accept `?authorized_only=true`) so JobPilot knows which jobs are authorized to click Submit vs preflight/draft only.

5. **Atomically consume authorization (Compare-And-Swap):** `POST /api/agent/jobs/{job_id}/submission-auth/consume`
   - **Request body:**
     ```json
     {
       "auth_id": "subauth_9f8e7d6c5b4a3210",
       "expected_version": 1,
       "run_id": "jp_run_20261124_001",
       "idempotency_key": "consume:jp_run_20261124_001:attempt_1",
       "agent_id": "jobpilot-worker-1",
       "target_url": "https://boards.example.com/jobs/12345",
       "resume_sha256": "..."
     }
     ```
   - **Atomic execution under per-job lock / SQLite `BEGIN IMMEDIATE`:**
     1. Check `config.job_search_paused` -> `423 Locked`.
     2. Load current authorization + active job record.
     3. **Idempotent replay check:** If `state == "consumed"` AND `auth_id` matches AND `consume_idempotency_key == req.idempotency_key` AND `consumed_by_run_id == req.run_id` -> return `200 OK` with `{"consumed": true, "replay": true, ...}` (safe across network retries!).
     4. **CAS & state checks:** Verify `auth_id` matches, `version == expected_version`, `state == "granted"`. On mismatch -> append `consume_rejected` audit entry -> return `409 Conflict` with `{reason, state, current_version}`.
     5. **Expiry check:** If `now >= expires_at` -> transition `granted -> expired`, increment `version`, append `expired` audit entry -> return `410 Gone`.
     6. **Live eligibility & artifact integrity check:** Re-verify job is not archived, `posting_status != "closed"`, `status not in (APPLIED, INTERVIEW, OFFER, REJECTED, PASSED, SUBMISSION_UNCONFIRMED)`, and on-disk `resume_sha256` (and `req.resume_sha256`, if sent) still match `scope.resume_sha256` -> otherwise append `consume_rejected` audit entry -> return `409 Conflict` (`scope_drift` or `job_ineligible`).
     7. **Commit single-use transition:** Set `state = "consumed"`, `version += 1`, `consumed_at = now`, `consumed_by_run_id`, `consumed_by_agent`, `consume_idempotency_key`, append `consumed` audit entry (plus `AgentEvent` on job), atomically commit -> return `200 OK` with `{"consumed": true, "replay": false, "auth_id": ..., "version": 2, "consumed_at": ..., "scope": ...}`.
   - **Why this closes the race:** Under `fcntl.flock` + `threading.Lock` (or SQLite `BEGIN IMMEDIATE`), two concurrent consumers serialize strictly; the first commits `version: 1 -> 2, state: granted -> consumed`, and the second sees `state == "consumed"` with a different `idempotency_key`/`run_id` and receives `409 Conflict`.

6. **Report execution outcome:** `POST /api/agent/jobs/{job_id}/submission-auth/outcome`
   *(Can serve as the canonical v2 outcome endpoint, or be unified with a hardened `POST /api/agent/jobs/{job_id}/application-result` accepting `auth_id` + `idempotency_key`.)*
   - **Request body:**
     ```json
     {
       "auth_id": "subauth_9f8e7d6c5b4a3210",
       "run_id": "jp_run_20261124_001",
       "idempotency_key": "outcome:jp_run_20261124_001:final",
       "outcome": "confirmed_submitted",
       "applied_at": "2026-11-24T22:05:12+00:00",
       "portal_url": "https://boards.example.com/applications/98765",
       "confirmation_id": "CONF-98765",
       "evidence_ref": "jobpilot://runs/jp_run_20261124_001/confirmation.png",
       "notes": "Verified confirmation banner and receipt email."
     }
     ```
   - **Atomic execution under lock:**
     - Requires `auth_id` to exist with `state == "consumed"` and matching `consumed_by_run_id == run_id` (prevents unauthorized/unconsumed status writes to `APPLIED`).
     - **Idempotent on `outcome_idempotency_key`:** Repeat call with same key returns `200 OK` with `{"replay": true}` without duplicating `status_history`, `notes`, `follow_up.notes`, or `agent_log`.
     - **Conflicting outcome on same `auth_id`:** Returns `409 Conflict` (except optionally allowing human-or-verifier upgrade `unconfirmed_submission -> confirmed_submitted` with fresh evidence if explicitly modeled).
     - **Single-transaction lifecycle update:**
       - `confirmed_submitted` -> transitions `Job.status` to `JobStatus.APPLIED` (only if `status in (NEW, SCORED, SUBMISSION_UNCONFIRMED)`; never downgrades `INTERVIEW`/`OFFER`/`REJECTED`), sets `applied_at` & 14-day follow-up if unset, records `portal_url` once.
       - `unconfirmed_submission` -> transitions `Job.status` to `JobStatus.SUBMISSION_UNCONFIRMED` (keeps off `ready_to_apply`, no `applied_at`).
       - `aborted_before_submit` / `failed_before_submit` -> leaves `Job.status` unchanged (`SCORED`); authorization remains `consumed` (single-use — never auto-re-armed; human must issue a new grant to retry).
     - Appends one `outcome_reported` audit entry and one `AgentEvent` atomically.

---

## 4. Where JSHQ's Applied / Status Lifecycle Double-Writes or Conflicts with an External Executor

### 4.1 Complete Verified Inventory of Lifecycle Writers in JSHQ

| Writer | Location | Trigger | Fields Written | Guards Present? |
| :--- | :--- | :--- | :--- | :--- |
| `Job.update_status()` | `models.py:204-225` | Helper used by all status writers below | `status`, `status_history`, `updated_at`, `applied_at`, `follow_up` | **None** (no transition validation; only `if not self.applied_at:`). |
| `PATCH /api/jobs/{job_id}` | `main.py:1100-1138` | Human UI | `status`, `applied_at`, `review_status`, `notes`, etc. | Can set `applied_at` without changing `status` (`main.py:1122-1129`); no lock. |
| `POST /api/jobs/apply-batch` | `main.py:883-905` | Human UI bulk action | `status=APPLIED`, `applied_at`, `follow_up` | No lock; no check if already applied/later stage. |
| `POST /api/jobs/{id}/application-result` | `main.py:1566-1588` | Session-auth writeback | `status`, `applied_at`, `notes`, `follow_up.notes` | No idempotency; no transition guard; no lock. |
| `POST /api/agent/jobs/{id}/application-result` | `main.py:1809-1833` | Agent-key writeback | `status`, `applied_at`, `notes`, `follow_up.notes` | No idempotency; no transition guard; no lock; no `agent_log`. |
| `POST /api/jobs/{id}/score` | `main.py:1333-1352` | Human UI single score | `score`, `status=SCORED`, `market_lane` | **No status guard** — resets `APPLIED`/`INTERVIEW`/`REJECTED` to `SCORED`! |
| `POST /api/jobs/score-batch` | `main.py:854-878` | Human UI batch score | `score`, `status=SCORED`, `market_lane` | **No status guard** — resets any job to `SCORED`! |
| `_run_score_all` (`POST /api/jobs/score-all`) | `main.py:718-747` | Background thread | `score`, `status=SCORED`, `market_lane` | Selects `NEW` **or** `score.total == 0` regardless of `status` (`main.py:725`) — resets `APPLIED` jobs with model score 0 back to `SCORED`! |
| `POST /api/agent/jobs/{id}/refresh-and-score` | `main.py:1891-1926` | Agent-key refresh | `raw_jd`, `score`, `status=SCORED`, `market_lane` | **No status guard** — resets any job to `SCORED`! |
| `POST /api/jobs/{id}/parse-confirmation` | `main.py:1958-2008` | Human UI pasted email | `status=INTERVIEW` (if `APPLIED`) or `REJECTED` (always!), `follow_up`, `emails` | `REJECTED` branch (`main.py:2001-2002`) has no status guard; overwrites `follow_up.notes` completely (`main.py:1992`). |
| `rejection_intake.run()` | `rejection_intake.py:167-225` | Nightly scheduler (`scheduler.py:529`) or `POST /api/inbox/scan-rejections` | `status=REJECTED`, `agent_log` | Checks `live.status == JobStatus.APPLIED` (`rejection_intake.py:210`), but no lock. |
| `auto_approve.run()` | `auto_approve.py:47-76` | Scheduler (`scheduler.py:521`), `backfill.py:127`, `PUT /api/config` (`main.py:2298`) | `review_status=APPROVED`, `agent_log` | Checks `PENDING` + `is_eligible`, but no lock. |
| `POST /api/digest-action` | `main.py:1070-1086` | Email link click | `review_status=APPROVED|REJECTED` | No status/closed/archive check; no `agent_log`; resurrects archived jobs. |
| `PATCH /api/agent/jobs/{id}/review` | `main.py:1836-1854` | Agent-key call | `review_status` | Allows agent self-approval; no `agent_log`; resurrects archived jobs. |
| `job_liveness.run_pass()` | `job_liveness.py:194-222` | Nightly 06:00 ET (`scheduler.py:130-137`) or `POST /api/jobs/check-postings` | `posting_status`, `posting_reason`, `posting_checked_at`, `posting_closed_at` | Holds whole `Job` snapshot across up to 20s HTTP fetch (`job_liveness.py:138, 206-212`) then calls whole-file `save_job()`. |
| `jd_cleanup.cleanup_all()` / `_run_cleanup_jds()` | `jd_cleanup.py:65-81`, `main.py:786-809` | Daily 05:30 ET (`scheduler.py:112-119`) or UI button | `raw_jd`, `title`, `pay_range` | Loads all jobs up front (`jd_cleanup.py:43`), holds stale snapshots across minutes of HTTP scraping, then calls whole-file `save_job()`. |
| `backfill.generate_missing()` / `research_batch.research_missing()` | `backfill.py:81-143`, `research_batch.py:101-143` | Daily pipeline or UI background thread | `tailored_resume*`, `cover_letters`, `research` | Holds whole `Job` snapshot across multi-second LLM/web calls, then calls whole-file `save_job()`. |
| `archiving.run_archive_pass()` | `archiving.py:81-114` | Daily 05:00 ET (`scheduler.py:121-128`) or `POST /api/archive/run` | Moves `{job_id}.json` from `jobs/` to `jobs_archive/` | Archives approved score `5–7` jobs after 3 days (`archiving.py:57-65`). |

### 4.2 Concrete Double-Write and Conflict Scenarios

1. **Non-idempotent retry & dual `application-result` endpoints (`main.py:1566-1588`, `main.py:1809-1833`):**
   - *Verified:* Both session-auth and agent-auth `application-result` endpoints unconditionally append to `status_history`, `notes`, `follow_up.notes`, and `error` notes.
   - *Conflict:* A JobPilot retry after an HTTP timeout—or a human clicking "Mark Applied" while JobPilot finishes—double-writes `status_history` (`applied -> applied`) and duplicates notes/portal URLs.

2. **Scoring paths reset `APPLIED` / `INTERVIEW` / `OFFER` / `REJECTED` back to `SCORED` (`main.py:725, 737, 869, 1344, 1917`):**
   - *Verified:* `POST /api/jobs/{id}/score`, `POST /api/jobs/score-batch`, `POST /api/agent/jobs/{id}/refresh-and-score`, and `_run_score_all` all call `job.update_status(JobStatus.SCORED)` unconditionally. Worse, `_run_score_all` (`main.py:725`) selects any job with `j.score is not None and j.score.total == 0` regardless of `j.status`.
   - *Conflict:* If a user overrides a 0-scored job to 9, JobPilot submits it (`status = APPLIED`), and later someone runs "Score All" (or re-scores/refreshes the job), JSHQ flips `status` back to `SCORED`. Because `review_status` is still `APPROVED` and docs exist on disk, **the already-applied job re-appears in `/api/agent/ready-queue` and risks double-submission.**

3. **Unchecked status downgrades from delayed executor callbacks (`models.py:204-225`, `main.py:1821`):**
   - *Verified:* Neither `Job.update_status()` nor `agent_record_application_result()` checks current `job.status`.
   - *Conflict:* If an instant auto-rejection email is processed by `rejection_intake.py:211` (setting `REJECTED`) or a human updates the job to `INTERVIEW` / `PASSED`, a delayed or retried JobPilot `POST /api/agent/jobs/{id}/application-result` (default `status=APPLIED`, `models.py:331`) **silently overwrites `REJECTED`/`INTERVIEW`/`PASSED` back to `APPLIED`**. Similarly, `parse_confirmation` (`main.py:1992`) overwrites `job.follow_up.notes` instead of merging, wiping out any `Portal: ...` note written by JobPilot (`main.py:1825-1828`).

4. **Whole-file lost-update clobber from background JSHQ threads (`storage.py:65-78`, `job_liveness.py:206-212`, `jd_cleanup.py:42-72`, `backfill.py:105-123`, `research_batch.py:122-123`):**
   - *Verified:* Every writer loads the entire `Job` JSON, performs slow network/LLM I/O (up to 15–120 seconds), and calls `storage.save_job()`, which overwrites the whole file without locking or version checking.
   - *Conflict:* During a nightly `job_liveness` pass (06:00 ET), `jd_cleanup` pass (05:30 ET), `research_batch`, or `backfill`, if JobPilot calls `PATCH /ats-url`, `POST /log`, or `POST /application-result` (`status = APPLIED`), the background thread finishes its HTTP/LLM call seconds later and **overwrites `{job_id}.json` with its pre-call `status = SCORED` snapshot**—silently erasing the `APPLIED` status, `ats_url`, and `agent_log` entries, and returning the job to `/api/agent/ready-queue`!

5. **Non-atomic multi-endpoint executor writeback (`main.py:1752-1833`):**
   - *Verified:* Today an executor must issue separate HTTP calls for `PATCH /ats-url`, `POST /log`, and `POST /application-result`.
   - *Conflict:* A crash or timeout after `POST /log` (`event_type=submit, success=True`) and before `POST /application-result` leaves `agent_log` claiming submission succeeded while `Job.status` remains `SCORED` and `ready_to_apply == True`.

6. **Archiving vs executor race & silent split-brain resurrection (`archiving.py:57-65`, `storage.py:65-87`):**
   - *Verified:* `archiving.below_tracking_threshold()` moves `NEW`/`SCORED` jobs scoring `< tracking_min_score` (default `8`) to `jobs_archive/` after 3 days even when `review_status == APPROVED`. If JobPilot (or a digest link) later writes to that `job_id`, `storage.load_job()` reads it from `jobs_archive/` (`storage.py:83-84`) and `storage.save_job()` writes a new copy into `jobs/` (`storage.py:76-77`) without removing `jobs_archive/{job_id}.json`.
   - *Conflict:* The job now exists in **both** `jobs/` and `jobs_archive/` with diverging state (`GET /api/jobs` sees one copy; `GET /api/jobs/archived` sees the stale copy).

7. **Approval ownership collisions (`main.py:1836-1854`, `auto_approve.py:47-76`):**
   - *Verified:* `PATCH /api/agent/jobs/{id}/review` allows an agent to mutate `review_status` directly, and `auto_approve.run()` automatically flips `PENDING -> APPROVED` on scheduler runs, backfill completion, and `PUT /api/config`.
   - *Conflict:* If an executor or operator sets `review_status = PENDING` to pause a problematic job, `auto_approve` can silently re-approve it back into `/api/agent/ready-queue` on the next backfill or config save.

8. **Artifact & cache conflicts (`docx_builder.py:143-146, 453-458`, `main.py:494-524`):**
   - *Verified:* Generated `.docx` filenames omit `job_id`, so two active jobs with matching `(company, title)` clobber each other's tailored résumé and cover letters on disk. Additionally, `_list_cache` (`main.py:494-524`) does not track `generated/` file changes, so the UI tracker can display `ready_to_apply: true` after a `.docx` file is deleted while `/api/agent/ready-queue` omits the job.

### 4.3 Minimal Conflict-Prevention Guardrails

1. **Enforce a validated status transition policy in `Job.update_status()` (`models.py:204-225`):**
   - Scoring (`NEW -> SCORED`) must only update `status` when `job.status == JobStatus.NEW` (never overwrite `APPLIED`, `INTERVIEW`, `OFFER`, `REJECTED`, `PASSED`, or `SUBMISSION_UNCONFIRMED`). Also exclude post-`SCORED` statuses in `_run_score_all` (`main.py:725`).
   - Executor writeback (`-> APPLIED` or `-> SUBMISSION_UNCONFIRMED`) must only transition from `NEW`, `SCORED`, or `SUBMISSION_UNCONFIRMED` (never downgrade `INTERVIEW`, `OFFER`, or `REJECTED`); human UI overrides remain allowed when explicitly flagged.
2. **Introduce locked read-modify-write helper `storage.mutate_job(user_id, job_id, fn)`:**
   - Acquires per-job `threading.Lock` + `fcntl.flock`, reloads fresh JSON from `jobs/` (refusing to resurrect `jobs_archive/` implicitly), runs `fn(job)` strictly for the narrow fields owned by that caller (after any slow network/LLM I/O has already finished!), and writes atomically via temp-file + `fsync` + `os.replace()`.
   - Refactor `job_liveness`, `jd_cleanup`, `backfill`, `research_batch`, `rejection_intake`, `auto_approve`, and API write routes so **no slow network/LLM I/O occurs between `load_job` and `save_job`**.
3. **Consolidate executor submission writeback into one atomic, idempotent outcome call:**
   - Update `submission_auth.outcome`, `Job.status`, `applied_at`, `follow_up`, `notes`, and `agent_log` inside one locked transaction keyed by `(auth_id, idempotency_key)`.
4. **Enforce single-owner boundaries:**
   - Remove agent ability to set `review_status = approved` (`main.py:1836-1854`).
   - Keep `posting_status` + `preflight` evidence separate from `Job.status`, and keep `submission_auth` separate from `review_status`.

---

## Appendix: Suggested Implementation Order (Non-Binding)

1. **Correctness & Safety Hotfixes (smallest blast radius):**
   - Fix cover-letter `.docx` generation in `scheduler.py:463-466` and disk-existence check in `Job.ready_to_apply` (`models.py:313`).
   - Add HTTP(S) URL check, `applied_at` check, `job_search_paused` check, `effective_total` sort, and `admin_id` 503 guards across `/api/agent/*`.
   - Guard all scoring paths (`main.py:725, 737, 869, 1344, 1917`) from resetting post-apply statuses; block `review_status = approved` on `PATCH /api/agent/jobs/{id}/review`.
   - Fix `docx_builder.py` filenames to include `job_id`; exempt `APPROVED` jobs from `archiving.below_tracking_threshold()` and include them in `job_liveness.due_jobs()`.
2. **Storage Atomicity & Locking:**
   - Add per-job locking (`threading.Lock` + `fcntl.flock`), atomic temp-file/`fsync`/`os.replace` writes in `storage.py`, `mutate_job()` pattern for background workers, and stop implicit archive resurrection in `save_job()`.
3. **Lifecycle & Agent Contract Extensions:**
   - Add `JobStatus.SUBMISSION_UNCONFIRMED`, idempotent/guarded `application-result`, `POST /api/agent/intake` (with dedup), `POST /api/agent/jobs/{id}/posting-evidence`, `GET /api/agent/jobs/{id}`, and extended `AgentEvent` schema.
4. **JSHQ-Owned Submission Authorization:**
   - Implement isolated persistent `submission_auth` store (SQLite WAL or locked per-job record), human-session Grant/Revoke/Inspect endpoints + UI controls, and agent-key Read/CAS-Consume/Report-Outcome endpoints with end-to-end concurrency and replay tests.
