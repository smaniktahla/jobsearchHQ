from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
from datetime import datetime, timedelta
import uuid


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


class CoverLetter(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    job_id: str
    variant: str  # "direct", "consultative", "brief"
    content: str
    docx_path: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


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
    """Record of an email sent or received for a job."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    direction: str = "sent"  # "sent" or "received"
    to: str = ""
    subject: str = ""
    body: str = ""
    attachments: list[str] = []
    sent_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str = ""
    company: str = ""
    url: str = ""
    source: str = ""
    intake_source: IntakeSource = IntakeSource.MANUAL_PASTE
    raw_jd: str = ""
    pay_range: str = ""
    market_lane: MarketLane = MarketLane.CONTRACT
    status: JobStatus = JobStatus.NEW
    score: Optional[ScoreBreakdown] = None
    cover_letters: list[CoverLetter] = []
    tailored_resume: str = ""
    tailored_resume_docx: str = ""  # path to generated .docx
    applied_at: str = ""
    follow_up: FollowUp = Field(default_factory=FollowUp)
    emails: list[EmailRecord] = []
    notes: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    status_history: list[dict] = []

    def update_status(self, new_status: JobStatus, applied_date: str = ""):
        self.status_history.append({
            "from": self.status,
            "to": new_status,
            "at": datetime.now().isoformat()
        })
        self.status = new_status
        self.updated_at = datetime.now().isoformat()

        if new_status == JobStatus.APPLIED:
            if applied_date:
                try:
                    dt = datetime.fromisoformat(applied_date)
                except ValueError:
                    dt = datetime.now()
            else:
                dt = datetime.now()
            if not self.applied_at:
                self.applied_at = dt.isoformat()
                self.follow_up.due_at = (dt + timedelta(days=14)).isoformat()
                self.follow_up.count = 0
                self.follow_up.sent_at = ""

    @property
    def follow_up_due(self) -> bool:
        if self.status not in (JobStatus.APPLIED, JobStatus.INTERVIEW):
            return False
        if not self.follow_up.due_at:
            return False
        try:
            due = datetime.fromisoformat(self.follow_up.due_at)
            return datetime.now() >= due
        except (ValueError, TypeError):
            return False

    @property
    def days_since_applied(self) -> int | None:
        if not self.applied_at:
            return None
        try:
            applied = datetime.fromisoformat(self.applied_at)
            return (datetime.now() - applied).days
        except (ValueError, TypeError):
            return None


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
    gut_interest: Optional[int] = None
    notes: Optional[str] = None
    applied_at: Optional[str] = None  # ISO date string for manual override


class EmailCompose(BaseModel):
    to: str
    subject: str
    body: str
    cc: str = ""
    attach_resume: bool = True


class AppConfig(BaseModel):
    w2_salary_min: int = 160000
    w2_salary_max: int = 220000
    contract_hourly_min: int = 85
    contract_hourly_max: int = 125
    follow_up_days: int = 14
    follow_up_email: str = "salil.maniktahla@gmail.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""  # Gmail App Password
    resume_labels: dict[str, str] = {
        "director": "Resume 1 — Director",
        "base": "Resume 2 — Base",
        "contract": "Resume 3 — Contract",
    }
    deal_breakers: list[str] = [
        "must relocate",
        "on-site only outside DC metro",
        "requires TS/SCI",
    ]
    preferred_keywords: list[str] = [
        "data architecture",
        "analytics",
        "BI",
        "ERP",
        "financial systems",
        "data engineering",
        "Python",
        "SQL",
        "Tableau",
        "Power BI",
    ]
    # --- Scheduled Search ---
    scheduled_search_enabled: bool = False
    scheduled_search_time: str = "07:00"  # HH:MM in ET
    scheduled_search_terms: list[str] = [
        "senior director data analytics",
        "data architecture manager",
    ]
    scheduled_search_sites: list[str] = ["indeed", "linkedin", "google"]
    scheduled_search_location: str = "Washington, DC"
    scheduled_search_results: int = 25
    scheduled_search_hours_old: int = 24
    scheduled_search_remote: bool = True
    # --- Automation Pipeline ---
    auto_score_new_jobs: bool = False
    auto_generate_above_threshold: bool = False
    auto_generate_threshold: int = 7  # score >= this triggers resume + cover letters
