from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from enum import Enum
from datetime import datetime, timedelta, timezone
import uuid
from pathlib import Path


def _aware(dt: datetime) -> datetime:
    """Legacy timestamps stored before timezone-aware timestamps were
    introduced are naive but were always wall-clock UTC (container runs
    UTC) — treat a naive value as UTC rather than crashing on comparison
    against an aware datetime.now(timezone.utc)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MarketLane(str, Enum):
    W2_SNIPER = "w2_sniper"
    CONTRACT = "contract"
    CONTRACT_TO_HIRE = "contract_to_hire"
    IGNORE = "ignore"


class JobStatus(str, Enum):
    NEW = "new"
    SCORED = "scored"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    PASSED = "passed"


class IntakeSource(str, Enum):
    MANUAL_PASTE = "manual_paste"
    EMAIL_FORWARD = "email_forward"
    URL_SCRAPE = "url_scrape"
    API = "api"


class User(BaseModel):
    """Represents an authenticated user derived from OIDC claims."""
    id: str        # OIDC 'sub' claim — stable, provider-agnostic
    email: str = ""
    name: str = ""


class ScoreBreakdown(BaseModel):
    skills_match: int = Field(ge=0, le=4, description="0-4: hard skill overlap")
    scope_impact: int = Field(ge=0, le=3, description="0-3: seniority/leadership match")
    pay_alignment: int = Field(ge=0, le=2, description="0-2: comp range vs target")
    gut_interest: int = Field(ge=0, le=1, default=0, description="0-1: manual override")
    total: int = Field(ge=0, le=10, default=0)
    skills_rationale: str = ""
    scope_rationale: str = ""
    pay_rationale: str = ""
    keyword_gaps: list[str] = []
    red_flags: list[str] = []
    bullshit_flag: bool = False
    bullshit_reason: str = ""
    recommended_resume: str = ""
    recommended_lane: MarketLane = MarketLane.IGNORE
    raw_analysis: str = ""
    model: str = ""   # provider/model that produced this score, e.g. "gemini/gemini-3.5-flash-lite"


class ScoreEntry(BaseModel):
    """One line of a job's score history: every model score, and every user change."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "model"          # "model" | "user" | "user_cleared"
    model: str = ""
    total: int = 0
    skills_match: Optional[int] = None
    scope_impact: Optional[int] = None
    pay_alignment: Optional[int] = None
    gut_interest: Optional[int] = None
    recommended_lane: str = ""
    recommended_resume: str = ""
    note: str = ""


class ScoreOverride(BaseModel):
    """The user's own score. Wins over the model's wherever a score is used; the model's stays on record."""
    total: int = Field(ge=0, le=10)
    note: str = ""
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CoverLetter(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    job_id: str
    variant: str  # "direct", "consultative", "brief"
    content: str
    docx_path: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class FollowUp(BaseModel):
    due_at: str = ""
    sent_at: str = ""
    count: int = 0
    contact_email: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    extracted_emails: list[str] = []
    extracted_links: list[str] = []
    notes: str = ""


class EmailRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    direction: str = "sent"  # "sent" or "received"
    to: str = ""
    subject: str = ""
    body: str = ""
    attachments: list[str] = []
    sent_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Contact(BaseModel):
    name: str = ""
    title: str = ""
    email: str = ""
    linkedin_url: str = ""
    notes: str = ""
    confidence: str = "medium"  # high | medium | low
    # Where this person came from: "web" (company research) and/or a people search source (see people_search.py).
    sources: list[str] = []
    provider_ids: dict[str, str] = {}   # source -> that source's person id (Apollo Reveal needs it)
    masked: bool = False                # name partly hidden until revealed (Apollo search results)


class CompanyResearch(BaseModel):
    contacts: list[Contact] = []
    company_summary: str = ""
    researched_at: str = ""
    searches_run: list[str] = []


class ReviewStatus(str, Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AgentEventType(str, Enum):
    SCORE      = "score"
    DISPATCH   = "dispatch"
    ESCALATE   = "escalate"
    RESOLVE    = "resolve"
    SUBMIT     = "submit"
    ERROR      = "error"
    APPLICATION_RESULT = "application_result"
    EVIDENCE = "evidence"


class ApplicationEvidence(BaseModel):
    url: str = Field(default="", max_length=2048)
    title: str = Field(default="", max_length=512)
    text_sha256: str = Field(default="", max_length=64)
    screenshot_sha256: str = Field(default="", max_length=64)
    report_hash: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=2048)


class PostingEvidence(BaseModel):
    kind: Literal["closed_posting", "accessibility_preflight"]
    verdict: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    observed_at: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=2048)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        if datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value


class AgentEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    event_type: AgentEventType
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes: str = ""
    ats: str = ""
    script: str = ""
    claude_tokens_in: int = 0
    claude_tokens_out: int = 0
    success: Optional[bool] = None
    run_id: str | None = Field(default=None, min_length=1, max_length=256)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    actor: str | None = Field(default=None, max_length=128)
    evidence: ApplicationEvidence | PostingEvidence | None = None


class UnconfirmedSubmission(BaseModel):
    run_id: str | None
    at: str
    evidence_ref: str


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    user_id: str = ""          # OIDC sub — set on creation, never changed
    title: str = ""
    company: str = ""
    url: str = ""
    source: str = ""
    intake_source: IntakeSource = IntakeSource.MANUAL_PASTE
    raw_jd: str = ""
    pay_range: str = ""
    market_lane: MarketLane = MarketLane.CONTRACT
    resume_lane_override: str = ""   # manual resume lane (director/base/contract); blank = use the scorer's
    status: JobStatus = JobStatus.NEW
    score: Optional[ScoreBreakdown] = None          # latest MODEL score (never edited by the user's override)
    score_history: list[ScoreEntry] = []             # every model score + user change, oldest first
    score_override: Optional[ScoreOverride] = None   # the user's own score, if they disagree with the model
    cover_letters: list[CoverLetter] = []
    tailored_resume: str = ""
    tailored_resume_docx: str = ""
    applied_at: str = ""
    submission_unconfirmed: UnconfirmedSubmission | None = None
    follow_up: FollowUp = Field(default_factory=FollowUp)
    emails: list[EmailRecord] = []
    research: Optional[CompanyResearch] = None
    notes: str = ""
    ats_url: str = ""          # direct ATS portal URL (set by dispatch.js)
    review_status: ReviewStatus = ReviewStatus.PENDING  # human go/no-go decision
    agent_log: list[AgentEvent] = []
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status_history: list[dict] = []
    # Posting liveness (see job_liveness.py) — independent of the application workflow status
    posting_status: str = ""        # "" = never checked | "open" | "closed" | "unknown"
    posting_reason: str = ""
    posting_checked_at: str = ""
    posting_closed_at: str = ""

    def update_status(self, new_status: JobStatus, applied_date: str = ""):
        self.status_history.append({
            "from": self.status,
            "to": new_status,
            "at": datetime.now(timezone.utc).isoformat()
        })
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc).isoformat()

        if new_status == JobStatus.APPLIED:
            if applied_date:
                try:
                    dt = _aware(datetime.fromisoformat(applied_date))
                except ValueError:
                    dt = datetime.now(timezone.utc)
            else:
                dt = datetime.now(timezone.utc)
            if not self.applied_at:
                self.applied_at = dt.isoformat()
                self.follow_up.due_at = (dt + timedelta(days=14)).isoformat()
                self.follow_up.count = 0
                self.follow_up.sent_at = ""

    # ── Scores: the model's, the user's override, and the history ─────────────
    @property
    def effective_total(self) -> Optional[int]:
        """The score everything should act on: the user's if they set one, else the model's."""
        if self.score_override is not None:
            return self.score_override.total
        return self.score.total if self.score is not None else None

    @property
    def score_is_user(self) -> bool:
        return self.score_override is not None

    @property
    def ai_skipped(self) -> bool:
        """The model put this job in the 'ignore' lane and the user hasn't overruled it with their own score."""
        return self.market_lane == MarketLane.IGNORE and self.score_override is None

    def _seed_score_history(self) -> None:
        """Jobs scored before history existed: record their current score as the first entry."""
        if not self.score_history and self.score is not None:
            s = self.score
            self.score_history.append(ScoreEntry(
                at=self.updated_at or datetime.now(timezone.utc).isoformat(), source="model",
                model=getattr(s, "model", "") or "", total=s.total, skills_match=s.skills_match,
                scope_impact=s.scope_impact, pay_alignment=s.pay_alignment, gut_interest=s.gut_interest,
                recommended_lane=getattr(s.recommended_lane, "value", str(s.recommended_lane)),
                recommended_resume=s.recommended_resume, note="earlier score (recorded before history was kept)"))

    def set_model_score(self, result: "ScoreBreakdown") -> None:
        """Store a new model score and keep it in the history. Never touches the user's override."""
        self._seed_score_history()
        self.score = result
        self.score_history.append(ScoreEntry(
            source="model", model=getattr(result, "model", "") or "", total=result.total,
            skills_match=result.skills_match, scope_impact=result.scope_impact,
            pay_alignment=result.pay_alignment, gut_interest=result.gut_interest,
            recommended_lane=getattr(result.recommended_lane, "value", str(result.recommended_lane)),
            recommended_resume=result.recommended_resume))

    def set_user_score(self, total: int, note: str = "") -> None:
        self._seed_score_history()
        self.score_override = ScoreOverride(total=total, note=note)
        self.score_history.append(ScoreEntry(source="user", total=total, note=note))

    def clear_user_score(self) -> None:
        if self.score_override is None:
            return
        self._seed_score_history()
        self.score_override = None
        self.score_history.append(ScoreEntry(
            source="user_cleared", total=self.score.total if self.score else 0, note="went back to the AI score"))

    @property
    def follow_up_due(self) -> bool:
        if self.status not in (JobStatus.APPLIED, JobStatus.INTERVIEW):
            return False
        if not self.follow_up.due_at:
            return False
        try:
            due = _aware(datetime.fromisoformat(self.follow_up.due_at))
            return datetime.now(timezone.utc) >= due
        except (ValueError, TypeError):
            return False

    @property
    def days_since_applied(self) -> int | None:
        if not self.applied_at:
            return None
        try:
            applied = _aware(datetime.fromisoformat(self.applied_at))
            return (datetime.now(timezone.utc) - applied).days
        except (ValueError, TypeError):
            return None

    @property
    def ready_to_apply(self) -> bool:
        """True when the job has a score, generated docs, the user has approved it,
        and it hasn't been actioned yet. review_status must be explicit APPROVED —
        score alone is never enough to greenlight an application."""
        return (
            self.effective_total is not None
            and self.effective_total >= 5
            and self.review_status == ReviewStatus.APPROVED
            and not self.ai_skipped
            and self.submission_unconfirmed is None
            and self.posting_status != "closed"
            and bool(self.tailored_resume_docx)
            and bool(self.cover_letters)
            and self.status not in (
                JobStatus.APPLIED, JobStatus.INTERVIEW,
                JobStatus.OFFER, JobStatus.REJECTED, JobStatus.PASSED
            )
            # Last, so the disk check only runs for jobs that pass everything else: a recorded path
            # whose file is gone (moved/deleted) doesn't count as having a tailored resume.
            and Path(self.tailored_resume_docx).exists()
        )


    claude_tokens_in: int = 0
    claude_tokens_out: int = 0
    success: Optional[bool] = None


class LegacyApplicationResult(BaseModel):
    """Written back by the agent after attempting to submit an application."""
    status: JobStatus = JobStatus.APPLIED
    notes: str = ""
    portal_url: str = ""
    applied_at: str = ""
    error: str = ""


class ApplicationResult(LegacyApplicationResult):
    run_id: str | None = Field(default=None, min_length=1, max_length=256)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    outcome: Literal["confirmed", "unconfirmed"] | None = None
    evidence: ApplicationEvidence | None = None

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(
            0xD800 <= ord(character) <= 0xDFFF for character in value
        )):
            raise ValueError("run_id must be nonblank and contain valid Unicode")
        return value


class JobCreate(BaseModel):
    title: str = ""
    company: str = ""
    url: str = ""
    source: str = ""
    raw_jd: str
    pay_range: str = ""
    notes: str = ""


class JobUpdate(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    url: Optional[str] = None
    pay_range: Optional[str] = None
    status: Optional[JobStatus] = None
    market_lane: Optional[MarketLane] = None
    resume_lane_override: Optional[str] = None
    review_status: Optional[ReviewStatus] = None
    gut_interest: Optional[int] = None
    notes: Optional[str] = None
    applied_at: Optional[str] = None


class WorkExperience(BaseModel):
    title: str = ""
    company: str = ""
    company_short: str = ""
    location: str = ""
    start: str = ""        # YYYY-MM
    end: str | None = None # YYYY-MM or null if current
    current: bool = False
    description: str = ""

LANGUAGE_LEVELS = ["Beginner", "Intermediate", "Proficient", "Fluent", "Native"]

class Language(BaseModel):
    language: str = ""
    speaking: str = "Beginner"
    reading: str = "Beginner"
    writing: str = "Beginner"

class Education(BaseModel):
    school: str = ""
    degree: str = ""
    degree_value: str = ""
    major: str = ""
    major_value: str = ""
    completed: bool = True
    grad_year: str = ""

class ProfileUpdate(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    linkedin: str = ""
    website: str = ""
    github: str = ""
    portfolio: str = ""
    work_experience: list[WorkExperience] | None = None
    education: Education | None = None
    certifications: list[str] | None = None
    languages: list[Language] | None = None


class EmailCompose(BaseModel):
    to: str
    subject: str
    body: str
    cc: str = ""
    attach_resume: bool = True


class AppConfig(BaseModel):
    network_enabled: bool = False
    network_ai_opt_in: bool = False
    application_strategy: str = "standard"
    network_ai_url: str = ""
    network_ai_model: str = ""

    @field_validator("network_ai_url")
    @classmethod
    def validate_network_ai_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        if value == "":
            return value
        try:
            url = urlsplit(value)
            valid = url.scheme in ("http", "https") and bool(url.hostname) and url.port != 0
        except ValueError:
            valid = False
        if not valid or url.username or url.password or any(c.isspace() for c in value):
            raise ValueError("Expected an HTTP(S) URL without credentials, or blank")
        return value

    # Scoring targets
    # 0 = not set. New accounts start with no personal data at all (see storage.load_config).
    w2_salary_min: int = 0
    w2_salary_max: int = 0
    contract_hourly_min: int = 0
    contract_hourly_max: int = 0
    # False only for accounts that have not finished the first-login setup wizard.
    # Existing config files predate the field, so they default to "done".
    onboarding_completed: bool = True
    # Optional strict first-pass cuts for thin LinkedIn-alert listings (email_intake). 0/False = off.
    prefilter_w2_floor: int = 0
    prefilter_hourly_floor: int = 0
    prefilter_dc_metro_only: bool = False
    prefilter_manager_plus_only: bool = False   # reject W2 titles below Manager level
    follow_up_days: int = 14
    # Author / applicant profile (used in generated documents and application packages)
    author_name: str = ""
    author_location: str = ""
    author_phone: str = ""
    author_email: str = ""   # convenience alias; smtp_user is the canonical send address
    author_address: str = ""
    author_city: str = ""
    author_state: str = ""
    author_zip: str = ""
    # IANA tz name (e.g. "America/New_York"). Explicit value always wins;
    # blank means "infer from author_state/author_location" — see
    # scheduler.effective_timezone(). Used for the scheduled search's
    # Run Time and for display purposes.
    timezone: str = ""
    author_linkedin: str = ""
    author_website: str = ""
    author_github: str = ""
    author_portfolio: str = ""
    work_experience: list[WorkExperience] = []
    education: Education = Education()
    certifications: list[str] = []
    languages: list[Language] = []
    # Email
    follow_up_email: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_html: bool = True   # send HTML digest emails (false = plain text only)
    # Resume variants
    resume_labels: dict[str, str] = {
        "director": "Resume 1 — Director",
        "base": "Resume 2 — Base",
        "contract": "Resume 3 — Contract",
    }
    # What each resume lane is for (see lanes.py); blank entries fall back to the built-in descriptions.
    resume_lane_descriptions: dict[str, str] = {}
    deal_breakers: list[str] = []
    preferred_keywords: list[str] = []
    # --- Scheduled Search ---
    scheduled_search_enabled: bool = False
    scheduled_search_time: str = "07:00"  # HH:MM in ET
    scheduled_search_terms: list[str] = []
    scheduled_search_sites: list[str] = ["indeed", "linkedin", "google"]
    scheduled_company_sites: list[dict[str, str]] = []
    scheduled_search_location: str = ""
    scheduled_search_results: int = 25
    scheduled_search_hours_old: int = 24
    scheduled_search_remote: bool = True    # "Remote only" — exclusive of hybrid/onsite
    scheduled_search_hybrid: bool = False
    scheduled_search_onsite: bool = False
    # --- Automation Pipeline ---
    job_search_paused: bool = False  # master kill switch: stops ingest, scoring, and generation
    auto_score_new_jobs: bool = False
    auto_generate_above_threshold: bool = False
    tracking_min_score: int = 8          # jobs scoring below this are archived and not live-checked
    posting_check_enabled: bool = True   # nightly open/closed check of tracked job postings
    posting_check_batch: int = 300       # max postings checked per run
    auto_generate_threshold: int = 7  # score >= this triggers resume + cover letters
    # Approve (=> ready to apply) scored jobs whose resume + cover letters exist. Off by default.
    auto_approve_enabled: bool = False
    auto_approve_threshold: int = 9
    auto_approve_daily_cap: int = 25
    # Nightly: mark Applied jobs Rejected when a matching rejection email is in the Gmail inbox. Off by default.
    inbox_rejections_enabled: bool = False
    # Research the company (domain + leadership contacts) for scored jobs at/above the threshold.
    auto_research_enabled: bool = False
    auto_research_threshold: int = 9
    auto_research_daily_cap: int = 25
    # --- People search (Settings → People Search; see people_search.py) ---
    # Ordered sources this account searches during company research: "pdl", "hunter", "apollo".
    # Each needs this account's own key; there is no shared server key. Empty = off.
    people_search_providers: list[str] = []
    people_search_mode: str = "first"          # "first": stop at the first source that finds people; "all"
    people_search_max_results: int = 8
    people_search_in_auto_research: bool = False   # automatic research spends credits only when this is on
    people_search_cache_days: int = 30
    # Who to look for. People search always starts from each job's own title; these add to it per account.
    people_search_titles: list[str] = []        # extra titles to look for at every company, e.g. "hiring manager"
    people_search_include_recruiters: bool = True
    pdl_api_key: str = ""
    hunter_api_key: str = ""
    apollo_api_key: str = ""
    # --- AI Provider ---
    # fast_provider: scoring, metadata, research (cheap/free tasks)
    # strong_provider: resumes, cover letters, LinkedIn messages (quality tasks)
    # Set them independently — e.g. Gemini for fast (free), Anthropic for strong (quality)
    fast_provider: str = "anthropic"    # "anthropic" | "gemini" | "openai" | "ollama"
    strong_provider: str = "anthropic"  # "anthropic" | "gemini" | "openai" | "ollama"
    anthropic_api_key: str = ""         # falls back to ANTHROPIC_API_KEY env var
    gemini_api_key: str = ""            # falls back to GEMINI_API_KEY env var
    openai_api_key: str = ""            # falls back to OPENAI_API_KEY env var
    openai_base_url: str = ""           # blank = api.openai.com; set for any OpenAI-compatible server
    fast_model: str = ""                # override default fast model
    strong_model: str = ""              # override default strong model
    ollama_url: str = ""
    ollama_model: str = ""
    # Large slow runs (score-all, resume backfill, daily pipeline) go here when batch_ollama_url is
    # set, so a big queue doesn't occupy the interactive local server. Blank = no split.
    batch_ollama_url: str = ""
    batch_ollama_model: str = ""
