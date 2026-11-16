#!/usr/bin/env python3
"""Create / reset the JSHQ demo account with fictional data.

Everything here is invented: a made-up person, made-up companies, example.test
addresses and 555-01xx phone numbers. No real person or employer appears.

Usage (inside the app container, from /app):
    python scripts/seed_demo.py         # create the demo user if missing, (re)seed its data
    python scripts/seed_demo.py --reset-password   # also issue a new password

Re-running wipes ONLY the demo user's jobs/config/resumes and rebuilds them, with dates
relative to today, so the demo always looks current. The password is printed once, when
it is created or reset; it is never stored in plain text.
"""
import secrets
import shutil
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import local_users  # noqa: E402
import storage  # noqa: E402
from models import (
    AppConfig, CompanyResearch, Contact, CoverLetter, EmailRecord, Education, FollowUp,
    IntakeSource, Job, JobStatus, Language, MarketLane, ReviewStatus, ScoreBreakdown,
    WorkExperience,
)

DEMO_EMAIL = "demo@jshq.example"
DEMO_NAME = "Alex Morgan (demo)"
NOW = datetime.now(timezone.utc)


def ago(days, hours=0):
    return (NOW - timedelta(days=days, hours=hours)).isoformat()


# ── Persona ────────────────────────────────────────────────────────────────────
CONFIG = AppConfig(
    onboarding_completed=True,
    author_name="Alex Morgan",
    author_location="Springfield, Demo State",
    author_phone="555-0142",
    author_email="alex.morgan@example.test",
    author_city="Springfield", author_state="DS", author_zip="00000",
    author_linkedin="https://linkedin.example.test/in/alex-morgan-demo",
    author_website="https://alexmorgan.example.test",
    timezone="America/New_York",
    w2_salary_min=165000, w2_salary_max=215000,
    contract_hourly_min=95, contract_hourly_max=130,
    work_experience=[
        WorkExperience(company="Brightwave Systems", title="Director, Data Platforms",
                       start="2021-03", end=None, current=True,
                       description="Lead a 22-person data engineering and analytics group. Cut warehouse "
                                   "cost 31% while doubling pipeline coverage; introduced data contracts "
                                   "and an on-call rotation."),
        WorkExperience(company="Harbor & Pine Logistics", title="Senior Manager, Analytics Engineering",
                       start="2016-06", end="2021-02",
                       description="Built the analytics engineering function from 3 to 14 people; "
                                   "migrated reporting to a cloud lakehouse."),
        WorkExperience(company="Kestrel Software", title="Lead Data Engineer",
                       start="2011-09", end="2016-05",
                       description="Designed batch and streaming pipelines for a SaaS analytics product."),
    ],
    education=Education(school="Demo State University", degree="Bachelor of Science", degree_value="bachelors", major="Computer Science", major_value="computer_science", completed=True, grad_year="2011"),
    certifications=["Cloud Data Engineer (demo)", "Certified Scrum Master (demo)"],
    languages=[Language(language="English", speaking="Native", reading="Native", writing="Native"),
               Language(language="Spanish", speaking="Intermediate", reading="Proficient", writing="Intermediate")],
    deal_breakers=["On-site 5 days", "Crypto exchange", "Commission-only"],
    preferred_keywords=["data platform", "lakehouse", "analytics engineering", "team leadership", "dbt", "Spark"],
    # Nothing that can spend money, send mail or reach the internet on a schedule.
    job_search_paused=True, scheduled_search_enabled=False, auto_score_new_jobs=False,
)

RESUMES = {
    "director": "ALEX MORGAN (DEMO)\nDirector, Data Platforms\n\nSUMMARY\nData leader with 14 years building platforms and "
                "teams. Led a 22-person group; cut warehouse cost 31%.\n\nEXPERIENCE\nBrightwave Systems - Director, Data Platforms (2021-present)\n"
                "- Lakehouse migration, data contracts, on-call rotation\nHarbor & Pine Logistics - Senior Manager, Analytics Engineering (2016-2021)\n"
                "- Grew team 3 to 14; cloud reporting migration\n\nEDUCATION\nB.S. Computer Science, Demo State University\n",
    "base": "ALEX MORGAN (DEMO)\nSenior Analytics Engineering Manager\n\nSUMMARY\nHands-on manager: dbt, Spark, cloud warehouses, mentoring.\n\n"
            "EXPERIENCE\nBrightwave Systems, Harbor & Pine Logistics, Kestrel Software (see LinkedIn).\n",
    "contract": "ALEX MORGAN (DEMO)\nData Platform Consultant (contract)\n\nSUMMARY\nAvailable for 3-12 month engagements: platform assessments, "
                "warehouse cost reduction, team stand-up.\n",
}

LANE = {"w2": MarketLane.W2_SNIPER, "c": MarketLane.CONTRACT, "cth": MarketLane.CONTRACT_TO_HIRE, "x": MarketLane.IGNORE}

# title, company, source, lane, status, (skills, scope, pay, gut), pay_range, created_days_ago, applied_days_ago, note
JOBS = [
    # ---- new (not scored yet)
    ("Head of Data Platform", "Larkspur Health", "LinkedIn", "w2", "new", None, "$210k-$250k", 1, None, ""),
    ("Director, Analytics Engineering", "Quillstone Bank", "Indeed", "w2", "new", None, "$190k-$230k", 1, None, ""),
    ("Principal Data Architect (contract)", "Nimbus Orchard", "Recruiter email", "c", "new", None, "$110-$135/hr", 2, None, ""),
    ("VP, Data & AI Platforms", "Meridian Freight Tech", "Company site", "w2", "new", None, "", 2, None, ""),
    ("Engineering Manager, Data Infrastructure", "Atlas Fern", "LinkedIn", "w2", "new", None, "$180k-$215k", 3, None, ""),
    ("Data Platform Lead (C2H)", "Copperline Energy", "Indeed", "cth", "new", None, "$100-$120/hr", 3, None, ""),
    # ---- scored
    ("Director of Data Engineering", "Willowmere Insurance", "LinkedIn", "w2", "scored", (4, 3, 2, 1), "$200k-$240k", 5, None, "Strong match; remote-friendly."),
    ("Senior Director, Data Platforms", "Stonebridge Media", "Company site", "w2", "scored", (4, 3, 1, 0), "$185k-$220k", 6, None, ""),
    ("Analytics Engineering Manager", "Pinecrest Retail", "Indeed", "w2", "scored", (3, 2, 2, 0), "$160k-$185k", 7, None, "Below target scope."),
    ("Data Strategy Consultant", "Driftwood Partners", "Recruiter email", "c", "scored", (3, 2, 2, 0), "$105-$125/hr", 8, None, ""),
    ("Staff Data Engineer", "Foxglove Labs", "LinkedIn", "w2", "scored", (3, 1, 1, 0), "$150k-$175k", 9, None, "IC track; scope mismatch."),
    ("Director, BI & Analytics", "Tidewater Utilities", "Indeed", "w2", "scored", (2, 3, 1, 0), "$150k-$180k", 10, None, "BI-heavy, little engineering."),
    ("Data Platform Manager", "Redwood Outfitters", "LinkedIn", "w2", "scored", (4, 2, 1, 1), "$170k-$195k", 11, None, ""),
    # ---- applied
    ("Director, Data Platforms", "Cobalt Harbor", "LinkedIn", "w2", "applied", (4, 3, 2, 1), "$205k-$245k", 24, 21, "Applied via company portal."),
    ("Senior Manager, Data Engineering", "Ironbark Systems", "Company site", "w2", "applied", (4, 2, 2, 0), "$175k-$205k", 20, 17, ""),
    ("Head of Analytics Engineering", "Saltmarsh Digital", "LinkedIn", "w2", "applied", (3, 3, 2, 0), "$180k-$210k", 16, 12, ""),
    ("Data Platform Consultant (6 mo)", "Halcyon Grid", "Recruiter email", "c", "applied", (4, 2, 2, 0), "$115-$135/hr", 12, 9, ""),
    ("Director of Data & Insights", "Marigold Foods", "Indeed", "w2", "applied", (3, 3, 1, 0), "$170k-$200k", 9, 5, ""),
    ("Engineering Manager, Data", "Silverleaf Mobility", "LinkedIn", "w2", "applied", (3, 2, 1, 0), "$165k-$190k", 6, 2, ""),
    # ---- interview / offer
    ("Director, Data Platform", "Verdant Cloud", "LinkedIn", "w2", "interview", (4, 3, 2, 1), "$215k-$255k", 38, 34, "Panel interview scheduled."),
    ("Senior Director, Data Engineering", "Lumen & Oak", "Recruiter email", "w2", "interview", (4, 3, 2, 0), "$225k-$260k", 31, 27, "Recruiter screen done; hiring manager next."),
    ("VP, Data Platform", "Northgate Robotics", "Company site", "w2", "offer", (4, 3, 2, 1), "$240k-$280k", 52, 48, "Verbal offer; written offer pending."),
    # ---- rejected / passed
    ("Director, Data Science Platform", "Bluefin Analytics", "LinkedIn", "w2", "rejected", (3, 3, 1, 0), "$190k-$220k", 45, 41, "Went with an internal candidate."),
    ("Data Platform Lead", "Orchard & Vine", "Indeed", "w2", "rejected", (3, 2, 1, 0), "$155k-$180k", 40, 36, ""),
    ("Director of Engineering (on-site)", "Granite Peak Mining", "Indeed", "x", "passed", (2, 2, 0, 0), "$170k-$190k", 22, None, "On-site 5 days: deal-breaker."),
    ("Data Engineer II", "Cinder Labs", "LinkedIn", "x", "passed", (2, 0, 0, 0), "$110k-$130k", 18, None, "Too junior."),
]

RESEARCH = {
    "Verdant Cloud": ("Mid-size cloud infrastructure vendor; data platform group of ~40 reporting to the CTO.",
                      [("Priya Raman", "VP Engineering", "priya.raman@verdantcloud.example.test", "high"),
                       ("Marcus Hale", "Talent Partner", "marcus.hale@verdantcloud.example.test", "medium")]),
    "Lumen & Oak": ("Venture-backed analytics software company; growing data platform org after Series C.",
                    [("Dana Whitfield", "Head of Talent", "dana.whitfield@lumenoak.example.test", "high")]),
    "Northgate Robotics": ("Warehouse robotics maker; data platform supports fleet telemetry and customer analytics.",
                           [("Tomas Eriksen", "CTO", "tomas.eriksen@northgaterobotics.example.test", "high"),
                            ("Hana Ito", "Recruiter", "hana.ito@northgaterobotics.example.test", "medium")]),
    "Cobalt Harbor": ("Port and shipping software provider; consolidating three data stacks into one platform.",
                      [("Leon Baptiste", "Director of People", "leon.baptiste@cobaltharbor.example.test", "medium")]),
}

LETTER = ("Dear {contact},\n\nI'm writing about the {title} role at {company}. Over the last four years I have led a "
          "22-person data platform group, cut warehouse cost by 31%, and introduced data contracts that halved "
          "pipeline incidents. I would welcome the chance to bring that approach to {company}.\n\nBest regards,\nAlex Morgan (demo)")


def jd(title, company, lane):
    kind = {"w2": "full-time", "c": "contract", "cth": "contract-to-hire", "x": "full-time"}[lane]
    return (f"{company} is hiring a {title} ({kind}). You will own the roadmap for our data platform, lead a team of "
            f"data and analytics engineers, and partner with product and finance.\n\nRequirements:\n- 8+ years in data "
            f"engineering or analytics engineering, 3+ leading teams\n- Cloud warehouse / lakehouse experience "
            f"(Snowflake, BigQuery or Databricks)\n- dbt, Spark or equivalent; strong SQL\n- Track record controlling "
            f"platform cost and reliability\n\n(Demo posting - fictional company and role.)")


def build_job(i, row):
    title, company, source, lane, status, parts, pay, created, applied, note = row
    intake = IntakeSource.EMAIL_FORWARD if source == "Recruiter email" else (
        IntakeSource.URL_SCRAPE if source == "Company site" else IntakeSource.API)
    job = Job(
        title=title, company=company, source=source, intake_source=intake,
        url=f"https://careers.{company.lower().replace(' & ', '').replace(' ', '')}.example.test/jobs/{1000 + i}",
        raw_jd=jd(title, company, lane), pay_range=pay, market_lane=LANE[lane],
        notes=note, created_at=ago(created), updated_at=ago(created),
        review_status=ReviewStatus.APPROVED if status in ("applied", "interview", "offer") else ReviewStatus.PENDING,
    )
    if parts:
        s, sc, p, g = parts
        total = min(10, s + sc + p + g)
        resume = "contract" if lane in ("c", "cth") else ("director" if sc == 3 else "base")
        job.score = ScoreBreakdown(
            skills_match=s, scope_impact=sc, pay_alignment=p, gut_interest=g, total=total,
            skills_rationale=f"{s}/4: " + ("Direct overlap on lakehouse, dbt and team leadership." if s >= 3 else "Partial overlap; role is more narrow."),
            scope_rationale=f"{sc}/3: " + ("Director-level ownership matches current scope." if sc >= 3 else "Scope below current level."),
            pay_rationale=f"{p}/2: " + ("Range meets or exceeds target." if p >= 2 else "Range is at or below target."),
            keyword_gaps=[] if s >= 4 else ["Databricks", "streaming"],
            red_flags=["Vague team structure"] if total <= 5 else [],
            recommended_resume=resume, recommended_lane=LANE[lane],
            raw_analysis="Demo score generated by the seed script.",
        )
        job.status = JobStatus.SCORED
        job.status_history = [{"from": "new", "to": "scored", "at": ago(created, -1)}]
    st = JobStatus(status)
    if st not in (JobStatus.NEW, JobStatus.SCORED):
        job.status_history = job.status_history or [{"from": "new", "to": "scored", "at": ago(created, -1)}]
        job.status = st
        if applied is not None:
            job.applied_at = ago(applied)
            job.status_history.append({"from": "scored", "to": "applied", "at": ago(applied)})
            job.follow_up = FollowUp(due_at=ago(applied - 14), count=0)
        if st in (JobStatus.INTERVIEW, JobStatus.OFFER):
            job.status_history.append({"from": "applied", "to": st.value, "at": ago(max(applied - 8, 1))})
        if st in (JobStatus.REJECTED, JobStatus.PASSED):
            job.status_history.append({"from": "scored" if st == JobStatus.PASSED else "applied", "to": st.value, "at": ago(max(created - 4, 1))})
        job.updated_at = ago(max((applied or created) - 3, 0))
    if company in RESEARCH:
        summary, contacts = RESEARCH[company]
        job.research = CompanyResearch(
            company_summary=summary, researched_at=ago(created - 1 if created > 1 else 0),
            contacts=[Contact(name=n, title=t, email=e, confidence=c, notes="Demo contact") for n, t, e, c in contacts])
        c0 = contacts[0]
        job.follow_up.contact_name, job.follow_up.contact_email = c0[0], c0[2]
    if status in ("applied", "interview", "offer") and i % 2 == 0:
        contact = (RESEARCH.get(company, ("", [("Hiring Team", "", "", "")]))[1][0][0])
        job.cover_letters = [CoverLetter(job_id=job.id, variant="direct",
                                         content=LETTER.format(contact=contact, title=title, company=company),
                                         created_at=ago(created - 1 if created > 1 else 0))]
    if status in ("interview", "offer"):
        job.emails = [
            EmailRecord(direction="sent", to=job.follow_up.contact_email or "recruiter@example.test",
                        subject=f"Application: {title}", body="Please find my application attached.", sent_at=ago(applied)),
            EmailRecord(direction="received", to="alex.morgan@example.test",
                        subject=f"Re: Application: {title}", body="Thanks Alex - we'd like to set up a conversation.",
                        sent_at=ago(max(applied - 3, 1))),
        ]
    return job


def main():
    reset_pw = "--reset-password" in sys.argv
    existing = next((u for u in local_users.list_users() if u["email"] == DEMO_EMAIL), None)
    password = None
    if existing is None:
        password = "demo-" + secrets.token_urlsafe(9)
        user = local_users.create_user(DEMO_NAME, DEMO_EMAIL, password)
    else:
        user = existing
        if reset_pw:
            password = "demo-" + secrets.token_urlsafe(9)
            local_users.set_password(user["id"], password)
    uid = user["id"]
    assert uid.startswith("u_") and user["email"] == DEMO_EMAIL, "refusing to touch a non-demo account"
    assert uid != "" and storage.get_user_dir(uid).name == uid

    base = storage.get_user_dir(uid)
    for sub in ("jobs", "jobs_archive", "cover_letters", "generated", "resumes"):
        shutil.rmtree(base / sub, ignore_errors=True)
    (base / "dedup_index.json").unlink(missing_ok=True)
    storage.ensure_user_dirs(uid)
    storage.save_config(uid, CONFIG)
    for variant, text in RESUMES.items():
        storage.save_resume_text(uid, variant, text)
    for i, row in enumerate(JOBS):
        storage.save_job(uid, build_job(i, row))

    n = len(storage.load_all_jobs(uid))
    print(f"demo user {uid} ({DEMO_EMAIL}): {n} jobs seeded")
    if password:
        print(f"LOGIN {DEMO_EMAIL} / {password}")
    else:
        print("password unchanged (use --reset-password to issue a new one)")


if __name__ == "__main__":
    main()
