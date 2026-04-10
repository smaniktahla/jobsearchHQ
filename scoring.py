import json
import os
import anthropic
from models import Job, ScoreBreakdown, CoverLetter, MarketLane, AppConfig
import storage


def get_client():
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


SCORING_SYSTEM_PROMPT = """You are a job search scoring assistant. You evaluate job descriptions against a candidate's background and return structured scores.

CANDIDATE BACKGROUND:
{full_history}

SCORING RUBRIC:
- skills_match (0-4): How well do the candidate's hard skills match the JD requirements? 4 = near-perfect overlap, 0 = completely different domain.
- scope_impact (0-3): Does the role's seniority, scope, and leadership expectations match the candidate's level? 3 = ideal match, 0 = way too junior or senior.
- pay_alignment (0-2): Based on the stated or inferred pay range vs candidate targets. 2 = within range, 1 = close, 0 = way off or unstated.

Candidate pay targets:
- W2: ${w2_min:,} - ${w2_max:,}/year
- Contract: ${hourly_min} - ${hourly_max}/hour

Deal breakers (auto-flag if present): {deal_breakers}

LANE ASSIGNMENT:
- Total 8-10 AND the role is W2/permanent → "w2_sniper"
- Total 5-7 OR the role is contract → "contract"  
- Contract-to-hire roles → "contract_to_hire"
- Total <5 → "ignore"

RESUME RECOMMENDATION:
- w2_sniper lane → "director" (leadership + architecture angle)
- contract lane → "contract" (keyword-heavy, fast to skim)
- contract_to_hire → "base" (general purpose)
- ignore → "none"

BULLSHIT FLAG: Set to true if the JD is vague, has MLM/pyramid indicators, unrealistic requirements, or obvious red flags (e.g., "entry level" requiring 15 years experience, unpaid, commission-only).

You MUST respond with ONLY valid JSON matching this exact structure (no markdown, no backticks):
{{
  "title_extracted": "string - job title from JD",
  "company_extracted": "string - company name from JD",
  "skills_match": 0,
  "scope_impact": 0,
  "pay_alignment": 0,
  "skills_rationale": "string - 2-3 sentences explaining skills score",
  "scope_rationale": "string - 2-3 sentences explaining scope score",
  "pay_rationale": "string - 1-2 sentences explaining pay score",
  "keyword_gaps": ["skill1", "skill2"],
  "red_flags": ["flag1"],
  "bullshit_flag": false,
  "bullshit_reason": "",
  "recommended_resume": "director|base|contract|none",
  "recommended_lane": "w2_sniper|contract|contract_to_hire|ignore",
  "raw_analysis": "string - 3-5 sentence overall assessment"
}}"""


def score_job(job: Job) -> ScoreBreakdown:
    """Score a job description against candidate background using Claude API."""
    client = get_client()
    config = storage.load_config()
    full_history = storage.load_resume_text("full_history")

    if not full_history:
        full_history = "(No full employment history loaded. Score based on JD quality only.)"

    system = SCORING_SYSTEM_PROMPT.format(
        full_history=full_history,
        w2_min=config.w2_salary_min,
        w2_max=config.w2_salary_max,
        hourly_min=config.contract_hourly_min,
        hourly_max=config.contract_hourly_max,
        deal_breakers=", ".join(config.deal_breakers),
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=system,
        messages=[
            {"role": "user", "content": f"Score this job description:\n\n{job.raw_jd}"}
        ],
    )

    raw = message.content[0].text.strip()
    # Clean potential markdown fencing
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ScoreBreakdown(
            skills_match=0, scope_impact=0, pay_alignment=0,
            raw_analysis=f"Failed to parse scoring response: {raw[:500]}"
        )

    total = data.get("skills_match", 0) + data.get("scope_impact", 0) + data.get("pay_alignment", 0)
    lane_str = data.get("recommended_lane", "ignore")
    try:
        lane = MarketLane(lane_str)
    except ValueError:
        lane = MarketLane.IGNORE

    return ScoreBreakdown(
        skills_match=min(data.get("skills_match", 0), 4),
        scope_impact=min(data.get("scope_impact", 0), 3),
        pay_alignment=min(data.get("pay_alignment", 0), 2),
        total=total,
        skills_rationale=data.get("skills_rationale", ""),
        scope_rationale=data.get("scope_rationale", ""),
        pay_rationale=data.get("pay_rationale", ""),
        keyword_gaps=data.get("keyword_gaps", []),
        red_flags=data.get("red_flags", []),
        bullshit_flag=data.get("bullshit_flag", False),
        bullshit_reason=data.get("bullshit_reason", ""),
        recommended_resume=data.get("recommended_resume", "none"),
        recommended_lane=lane,
        raw_analysis=data.get("raw_analysis", ""),
    )


COVER_LETTER_SYSTEM = """You are a cover letter writer. Generate a cover letter for a job application.

CANDIDATE RESUME (the variant being used for this application):
{resume_text}

SCORING ANALYSIS:
{score_analysis}

Write a cover letter in the requested style. Keep it to 3-4 paragraphs. Be specific about how the candidate's experience matches the role. Reference concrete accomplishments from the resume. Do not be generic or sycophantic.

Return ONLY the cover letter text, no preamble or commentary."""


def generate_cover_letters(job: Job) -> list[CoverLetter]:
    """Generate 3 cover letter variants for a scored job."""
    client = get_client()

    resume_variant = job.score.recommended_resume if job.score else "base"
    resume_text = storage.load_resume_text(resume_variant)
    if not resume_text:
        resume_text = storage.load_resume_text("base")
    if not resume_text:
        resume_text = "(No resume text loaded)"

    score_analysis = ""
    if job.score:
        score_analysis = f"""Skills Match: {job.score.skills_match}/4 - {job.score.skills_rationale}
Scope/Impact: {job.score.scope_impact}/3 - {job.score.scope_rationale}
Keyword Gaps: {', '.join(job.score.keyword_gaps) if job.score.keyword_gaps else 'None'}
Overall: {job.score.raw_analysis}"""

    system = COVER_LETTER_SYSTEM.format(
        resume_text=resume_text,
        score_analysis=score_analysis,
    )

    styles = {
        "direct": "Write a confident, direct cover letter. Lead with the strongest qualification match. Assertive tone. This person knows their value.",
        "consultative": "Write a consultative cover letter. Frame the candidate as a problem-solver who understands the employer's challenges. Collaborative tone.",
        "brief": "Write a brief, punchy cover letter — 2 paragraphs max. Get to the point fast. Ideal for email applications where brevity wins.",
    }

    letters = []
    for variant, instruction in styles.items():
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system,
            messages=[
                {"role": "user", "content": f"Job Description:\n{job.raw_jd}\n\nCompany: {job.company}\nTitle: {job.title}\n\nStyle: {instruction}"}
            ],
        )
        letters.append(CoverLetter(
            job_id=job.id,
            variant=variant,
            content=message.content[0].text.strip(),
        ))

    return letters


def extract_job_metadata(raw_jd: str) -> dict:
    """Use Claude to extract title, company, pay range from raw JD text."""
    client = get_client()
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[
            {"role": "user", "content": f"""Extract the following from this job posting. Return ONLY valid JSON, no markdown:
{{"title": "job title", "company": "company name", "pay_range": "pay range if stated, empty string if not", "is_contract": true/false, "is_w2": true/false}}

Job posting:
{raw_jd[:3000]}"""}
        ],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {}


TAILORED_RESUME_SYSTEM = """You are a resume tailoring expert. You take a candidate's base resume and full background, then produce a version precisely targeted to a specific job description.

FULL CANDIDATE BACKGROUND (draw from any of this):
{full_history}

BASE RESUME TEMPLATE (use this structure and formatting as the starting point):
{resume_text}

SCORING ANALYSIS FOR THIS JOB:
{score_analysis}

INSTRUCTIONS:
1. Keep the same section structure as the base resume template (Summary, Core Capabilities, Professional Experience, etc.)
2. Rewrite the Summary to directly address what this role needs — lead with the most relevant qualification
3. Reorder and rewrite Core Capabilities to front-load skills the JD explicitly asks for
4. For each role in Professional Experience, reorder bullets so the most JD-relevant accomplishments come first. Reword bullets to use the JD's language where truthful.
5. If the JD asks for something the candidate has done but the base resume doesn't mention, pull it from the full background
6. Address keyword gaps honestly — if the candidate has adjacent experience, surface it. If they genuinely lack something, don't fabricate it.
7. Keep the same tone, length, and professional voice as the base resume
8. Include all sections from the base resume (Earlier Experience, Technical Skills, Certifications, Education)

Output the complete tailored resume as clean text with clear section headers. No markdown formatting, no commentary — just the resume."""


def generate_tailored_resume(job: Job) -> str:
    """Generate a resume tailored to a specific job description."""
    client = get_client()

    resume_variant = job.score.recommended_resume if job.score else "base"
    resume_text = storage.load_resume_text(resume_variant)
    if not resume_text:
        resume_text = storage.load_resume_text("base")
    if not resume_text:
        resume_text = storage.load_resume_text("full_history")
    if not resume_text:
        raise ValueError("No resume text loaded. Upload at least one variant in Settings.")

    full_history = storage.load_resume_text("full_history")
    if not full_history:
        full_history = resume_text

    score_analysis = ""
    if job.score:
        score_analysis = f"""Skills Match: {job.score.skills_match}/4 - {job.score.skills_rationale}
Scope/Impact: {job.score.scope_impact}/3 - {job.score.scope_rationale}
Keyword Gaps: {', '.join(job.score.keyword_gaps) if job.score.keyword_gaps else 'None'}
Red Flags: {', '.join(job.score.red_flags) if job.score.red_flags else 'None'}
Recommended Lane: {job.score.recommended_lane}
Overall: {job.score.raw_analysis}"""

    system = TAILORED_RESUME_SYSTEM.format(
        full_history=full_history,
        resume_text=resume_text,
        score_analysis=score_analysis,
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=system,
        messages=[
            {"role": "user", "content": f"Tailor my resume for this job:\n\nCompany: {job.company}\nTitle: {job.title}\n\nJob Description:\n{job.raw_jd}"}
        ],
    )

    return message.content[0].text.strip()
