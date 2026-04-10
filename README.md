# Job Search Command Center

<p align="center">
  <img src="logo.png" alt="Job Search HQ" width="200">
</p>

Self-hosted job search pipeline: multi-board scraping, AI-powered scoring, tailored resume/cover letter generation, email integration, and application tracking.

![Dashboard](https://img.shields.io/badge/stack-FastAPI%20%2B%20Vanilla%20JS-blue) ![Python](https://img.shields.io/badge/python-3.11-green) ![License](https://img.shields.io/badge/license-MIT-lightgrey)

## Features

- **Multi-board job search** — Scrape Indeed, LinkedIn, Google, Glassdoor, ZipRecruiter from a single interface via [python-jobspy](https://github.com/speedyapply/JobSpy). Per-board status tracking, deduplication, parallel search.
- **AI scoring** — Claude API scores each job against your employment history. Skills match (0–4), scope/impact (0–3), pay alignment (0–2), gut interest (0–1). Auto-assigns to W2 Sniper / Contract / Ignore lanes.
- **Tailored resumes** — Generates a resume customized to each specific job description, output as a downloadable `.docx` in your template format.
- **Cover letters** — 3 variants per job (direct, consultative, brief) as downloadable `.docx` files.
- **Email integration** — Send applications with `.docx` attachments via Gmail SMTP. Parse confirmation emails to extract recruiter contact info.
- **Follow-up tracking** — Auto-schedules follow-ups when you mark "Applied." Snooze, mark done, email digest via cron.
- **Application tracker** — Pipeline view: New → Scored → Applied → Interview → Offer. Filter by lane, status, follow-up due.

## Screenshots

<p align="center">
  <img src="docs/dashboard.png" alt="Dashboard" width="800"><br>
  <em>Dashboard — pipeline funnel, score distribution, recent jobs at a glance</em>
</p>

<p align="center">
  <img src="docs/screenshot_search.png" alt="Search Boards" width="800"><br>
  <em>Search Boards — scrape Indeed, LinkedIn, Google and more with per-board status</em>
</p>

<p align="center">
  <img src="docs/screenshot_detail.png" alt="Job Detail" width="800"><br>
  <em>Job Detail — AI score breakdown, tailored resume generation, cover letters, email</em>
</p>

<p align="center">
  <img src="docs/screenshot_tracker.png" alt="Tracker" width="800"><br>
  <em>Tracker — filter by lane and status, follow-up tracking, applied dates</em>
</p>

## Quick Start

```bash
git clone https://github.com/smaniktahla/jobsearchHQ.git
cd jobsearchHQ

# Configure
cp .env.example .env
# Edit .env and add your Anthropic API key:
#   ANTHROPIC_API_KEY=sk-ant-...

# Launch
docker compose up -d --build

# Access at http://localhost:8093
```

## First Run Setup

1. **Settings** → Set your pay targets (W2 salary range, contract hourly rate)
2. **Settings** → Add Gmail credentials (email + [App Password](https://myaccount.google.com/apppasswords)) for sending applications
3. **Settings** → Upload your "Full Employment History" text (used for AI scoring context)
4. **Settings** → Optionally upload resume variant text (Director, Base, Contract) for cover letter generation
5. **Search Boards** → Run your first search

## Architecture

```
├── main.py              # FastAPI app (25+ endpoints)
├── models.py            # Pydantic data models
├── scoring.py           # Claude API: scoring, cover letters, tailored resumes
├── jobspy_search.py     # python-jobspy multi-board search wrapper
├── docx_builder.py      # .docx generation (resumes + cover letters)
├── email_service.py     # Gmail SMTP send + confirmation email parser
├── linkedin_intake.py   # Browser extension intake (optional)
├── intake.py            # Pluggable intake handlers (manual/URL/email)
├── storage.py           # JSON file persistence
├── static/
│   └── index.html       # Complete SPA frontend (dark theme)
├── data/
│   ├── jobs/            # One JSON file per job (gitignored)
│   ├── resumes/         # Resume text files (gitignored)
│   └── generated/       # Generated .docx files (gitignored)
├── Dockerfile
├── docker-compose.yaml
└── requirements.txt
```

## Scoring Rubric

| Dimension | Range | What it measures |
|-----------|-------|-----------------|
| Skills Match | 0–4 | Hard skill overlap with JD |
| Scope/Impact | 0–3 | Seniority and leadership fit |
| Pay Alignment | 0–2 | Comp range vs your targets |
| Gut Interest | 0–1 | Your manual assessment |

**Lane assignment:** 8–10 → W2 Sniper, 5–7 → Contract, <5 → Ignore

## Job Board Notes

| Board | Status | Notes |
|-------|--------|-------|
| Indeed | ✅ Best | No rate limiting, full descriptions, salary data |
| LinkedIn | ✅ Works | Rate limits ~page 10 without proxies. `linkedin_fetch_description` gets full JDs (slower) |
| Google | ⚠️ Varies | Needs specific search syntax, auto-generated from your query |
| Glassdoor | ❌ Broken | Glassdoor API changes broke the JobSpy scraper |
| ZipRecruiter | ⚠️ Varies | Results depend heavily on search terms |

**Location format:** Use standard format like `Washington, DC` or `New York, NY`. LinkedIn metro names (`Washington DC-Baltimore Area`) won't work for other boards.

## Automated Follow-Up Digest

Add to crontab for daily email reminders at 9 AM:

```bash
0 9 * * * curl -s -X POST http://localhost:8093/api/follow-ups/send-digest > /dev/null
```

## API Endpoints

<details>
<summary>Click to expand</summary>

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/search` | POST | Search job boards via JobSpy |
| `/api/jobs` | GET | List all jobs (filter by status/lane) |
| `/api/intake` | POST | Add job via manual/URL/email intake |
| `/api/jobs/{id}` | GET/PATCH/DELETE | Job CRUD |
| `/api/jobs/{id}/score` | POST | Score a job with Claude |
| `/api/jobs/{id}/tailored-resume` | POST | Generate tailored resume + .docx |
| `/api/jobs/{id}/download-resume` | GET | Download generated .docx resume |
| `/api/jobs/{id}/cover-letters` | POST | Generate 3 cover letter variants |
| `/api/jobs/{id}/cover-letters/{lid}/download` | GET | Download cover letter .docx |
| `/api/jobs/{id}/send-email` | POST | Send email with attachments |
| `/api/jobs/{id}/parse-confirmation` | POST | Parse confirmation email |
| `/api/jobs/{id}/mark-followed-up` | POST | Mark follow-up complete |
| `/api/jobs/{id}/snooze-follow-up` | POST | Snooze follow-up N days |
| `/api/jobs/score-batch` | POST | Batch score multiple jobs |
| `/api/follow-ups` | GET | Get due/upcoming follow-ups |
| `/api/follow-ups/send-digest` | POST | Send follow-up email digest |
| `/api/config` | GET/PUT | App configuration |
| `/api/resumes` | GET | List loaded resume variants |
| `/api/resumes/{variant}` | PUT | Upload resume text |
| `/api/test-email` | POST | Test SMTP configuration |

</details>

## Tech Stack

- **Backend:** FastAPI (Python 3.11)
- **Frontend:** Vanilla HTML/JS/CSS (no build step)
- **AI:** Anthropic Claude API (Sonnet)
- **Scraping:** [python-jobspy](https://github.com/speedyapply/JobSpy)
- **Documents:** python-docx for .docx generation
- **Storage:** JSON files (no database)
- **Deploy:** Docker

## Acknowledgments

- **[JobSpy](https://github.com/speedyapply/JobSpy)** by [speedyapply](https://github.com/speedyapply) — Multi-board job scraping library that powers the Search Boards feature. MIT licensed.
- **[Anthropic Claude API](https://www.anthropic.com)** — AI scoring, resume tailoring, and cover letter generation.
- **[python-docx](https://python-docx.readthedocs.io/)** — Word document generation for resumes and cover letters.

## License

MIT
