"""
Scoring, resume generation, and cover letter generation for Job Search HQ.

Supports two backends:
  - "local" (default): Ollama on AI box for scoring & metadata extraction (free)
  - "api": Anthropic Claude API (paid, Haiku for scoring, Sonnet for generation)

Resume and cover letter generation always use Anthropic API (Sonnet) when
backend is "api", or Ollama when backend is "local".
"""

import json
import logging
import os
import httpx
import anthropic
from models import Job, ScoreBreakdown, CoverLetter, MarketLane, AppConfig
import storage
import ai_router

logger = logging.getLogger(__name__)

# --- Anthropic API client ---

def get_client():
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


# Model tiers for Anthropic API
MODEL_FAST = "claude-haiku-4-5-20251001"    # scoring, metadata extraction
MODEL_STRONG = "claude-sonnet-4-5"   # resumes, cover letters

# Default Ollama settings (overridden by AppConfig)
DEFAULT_OLLAMA_URL = "http://10.10.10.105:11434"
DEFAULT_OLLAMA_MODEL = "gemma4:e4b"


def _is_local(config: AppConfig, tier: str = "fast") -> bool:
    """Check if a task tier should use local (Ollama) backend."""
    if tier == "creative":
        return getattr(config, "creative_backend", "api") == "local"
    # fast tier: check fast_backend first, fall back to scoring_backend for compat
    return getattr(config, "fast_backend", getattr(config, "scoring_backend", "local")) == "local"


# --- Ollama helper ---

def ollama_chat(system: str, user_msg: str, config: AppConfig) -> str:
    """Call Ollama's OpenAI-compatible chat endpoint and return the text response."""
    url = (config.ollama_url or DEFAULT_OLLAMA_URL).rstrip("/") + "/v1/chat/completions"
    model = config.ollama_model or DEFAULT_OLLAMA_MODEL

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_msg})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 2000,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except httpx.ConnectError:
        raise ConnectionError(
            f"Cannot reach Ollama at {url}. Is Ollama running on the AI box?"
        )
    except Exception as e:
        raise RuntimeError(f"Ollama call failed: {e}")


def anthropic_chat(system: str, user_msg: str, model: str, max_tokens: int = 2000) -> str:
    """Call Anthropic API and return the text response."""
    client = get_client()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return message.content[0].text.strip()


def clean_json_response(raw: str) -> str:
    """Extract JSON from an LLM response, handling markdown fencing, thinking tokens, etc."""
    raw = raw.strip()

    # Strip markdown code fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:]
    raw = raw.strip()

    # If it's already valid JSON, return it
    try:
        json.loads(raw)
        return raw
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find a JSON object embedded in the response
    # Look for the first { and last } to extract the JSON object
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace:last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, ValueError):
            pass

    logger.warning(f"Could not extract JSON from response: {raw[:200]}...")
    return raw


def safe_int(val, default=0) -> int:
    """Coerce a value to int, handling strings and floats from LLM output."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# === SCORING ===

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
  "raw_analysis": "string - 3-5 sentence overall assessment",
  "recommended_resume": "director|base|contract|none",
  "recommended_lane": "w2_sniper|contract|contract_to_hire|ignore",
  "keyword_gaps": ["skill1", "skill2"],
  "red_flags": ["flag1"],
  "bullshit_flag": false,
  "bullshit_reason": ""
}}"""


def score_job(job: Job, user_id: str = None) -> ScoreBreakdown:
    """Score a job description against candidate background."""
    config = storage.load_config(user_id)
    full_history = storage.load_resume_text(user_id, "full_history")

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

    user_msg = f"Score this job description:\n\n{job.raw_jd}"

    # Local models benefit from a schema reminder at the end of the user message
    if _is_local(config, "fast"):
        user_msg += """

IMPORTANT: Respond with ONLY valid JSON using EXACTLY these keys and value types:
{
  "skills_match": <integer 0-4>,
  "scope_impact": <integer 0-3>,
  "pay_alignment": <integer 0-2>,
  "skills_rationale": "<string>",
  "scope_rationale": "<string>",
  "pay_rationale": "<string>",
  "raw_analysis": "<string>",
  "recommended_resume": "director|base|contract|none",
  "recommended_lane": "w2_sniper|contract|contract_to_hire|ignore",
  "keyword_gaps": ["<string>", ...],
  "red_flags": ["<string>", ...],
  "bullshit_flag": <true/false>,
  "bullshit_reason": "<string>"
}
No markdown. No explanation. No extra keys. ONLY the JSON object."""

    # Route to backend
    backend_name = "ollama" if _is_local(config, "fast") else "api"
    logger.info(f"Scoring job '{job.title}' at '{job.company}' via {backend_name}")

    if _is_local(config, "fast"):
        raw = ollama_chat(system, user_msg, config)
    else:
        raw = anthropic_chat(system, user_msg, MODEL_FAST)

    logger.debug(f"Raw scoring response ({len(raw)} chars): {raw[:300]}...")
    raw = clean_json_response(raw)

    try:
        data = json.loads(raw)
        logger.info(f"Parsed scoring JSON OK: skills={data.get('skills_match')}, scope={data.get('scope_impact')}, pay={data.get('pay_alignment')}")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse scoring JSON: {e}\nRaw: {raw[:500]}")
        return ScoreBreakdown(
            skills_match=0, scope_impact=0, pay_alignment=0,
            raw_analysis=f"Failed to parse scoring response: {raw[:500]}"
        )

    sm = safe_int(data.get("skills_match"), 0)
    si = safe_int(data.get("scope_impact"), 0)
    pa = safe_int(data.get("pay_alignment"), 0)
    total = min(sm, 4) + min(si, 3) + min(pa, 2)

    lane_str = str(data.get("recommended_lane", "ignore")).lower().strip()
    # Map common LLM variations to our enum values
    lane_map = {
        "w2_sniper": MarketLane.W2_SNIPER,
        "w2 sniper": MarketLane.W2_SNIPER,
        "w2": MarketLane.W2_SNIPER,
        "apply": MarketLane.W2_SNIPER,
        "strong_apply": MarketLane.W2_SNIPER,
        "contract": MarketLane.CONTRACT,
        "contract_to_hire": MarketLane.CONTRACT_TO_HIRE,
        "c2h": MarketLane.CONTRACT_TO_HIRE,
        "ignore": MarketLane.IGNORE,
        "skip": MarketLane.IGNORE,
        "pass": MarketLane.IGNORE,
        "none": MarketLane.IGNORE,
    }
    lane = lane_map.get(lane_str, MarketLane.IGNORE)
    # If not in map, try the enum directly
    if lane_str not in lane_map:
        try:
            lane = MarketLane(lane_str)
        except ValueError:
            lane = MarketLane.IGNORE

    return ScoreBreakdown(
        skills_match=min(sm, 4),
        scope_impact=min(si, 3),
        pay_alignment=min(pa, 2),
        total=total,
        skills_rationale=str(data.get("skills_rationale", "")),
        scope_rationale=str(data.get("scope_rationale", "")),
        pay_rationale=str(data.get("pay_rationale", "")),
        keyword_gaps=data.get("keyword_gaps", []),
        red_flags=data.get("red_flags", []),
        bullshit_flag=bool(data.get("bullshit_flag", False)),
        bullshit_reason=str(data.get("bullshit_reason", "")),
        recommended_resume=str(data.get("recommended_resume", "none")),
        recommended_lane=lane,
        raw_analysis=str(data.get("raw_analysis", "")),
    )


# === METADATA EXTRACTION ===

def extract_job_metadata(raw_jd: str, user_id: str = None) -> dict:
    """Extract title, company, pay range from raw JD text."""
    config = storage.load_config(user_id) if user_id else AppConfig()

    user_msg = f"""Extract the following from this job posting. Return ONLY valid JSON, no markdown:
{{"title": "job title", "company": "company name", "pay_range": "pay range if stated, empty string if not", "is_contract": true/false, "is_w2": true/false}}

Job posting:
{raw_jd[:3000]}"""

    raw = ai_router.chat("", user_msg, "fast", config, max_tokens=500)

    raw = clean_json_response(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# === COVER LETTERS ===

COVER_LETTER_SYSTEM = """You are a cover letter writer. Generate a cover letter for a job application.

CANDIDATE RESUME (the variant being used for this application):
{resume_text}

SCORING ANALYSIS:
{score_analysis}

Write a cover letter in the requested style. Keep it to 3-4 paragraphs. Be specific about how the candidate's experience matches the role. Reference concrete accomplishments from the resume. Do not be generic or sycophantic.

Return ONLY the cover letter text, no preamble or commentary."""


def generate_cover_letters(job: Job, user_id: str = None) -> list[CoverLetter]:
    """Generate 3 cover letter variants for a scored job."""
    config = storage.load_config(user_id) if user_id else AppConfig()

    resume_variant = job.score.recommended_resume if job.score else "base"
    resume_text = storage.load_resume_text(user_id, resume_variant)
    if not resume_text:
        resume_text = storage.load_resume_text(user_id, "base")
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
        user_msg = f"Job Description:\n{job.raw_jd}\n\nCompany: {job.company}\nTitle: {job.title}\n\nStyle: {instruction}"

        text = ai_router.chat(system, user_msg, "strong", config, max_tokens=1500)

        letters.append(CoverLetter(
            job_id=job.id,
            variant=variant,
            content=text,
        ))

    return letters


# === TAILORED RESUME ===

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


def generate_tailored_resume(job: Job, user_id: str = None) -> str:
    """Generate a resume tailored to a specific job description."""
    config = storage.load_config(user_id) if user_id else AppConfig()

    resume_variant = job.score.recommended_resume if job.score else "base"
    resume_text = storage.load_resume_text(user_id, resume_variant)
    if not resume_text:
        resume_text = storage.load_resume_text(user_id, "base")
    if not resume_text:
        resume_text = storage.load_resume_text(user_id, "full_history")
    if not resume_text:
        raise ValueError("No resume text loaded. Upload at least one variant in Settings.")

    full_history = storage.load_resume_text(user_id, "full_history")
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

    user_msg = f"Tailor my resume for this job:\n\nCompany: {job.company}\nTitle: {job.title}\n\nJob Description:\n{job.raw_jd}"

    return ai_router.chat(system, user_msg, "strong", config, max_tokens=4000)


# ── LinkedIn message generation ────────────────────────────────────────────────

def generate_linkedin_message(
    job,
    contact_name: str,
    contact_title: str,
    author_name: str,
    user_id: str = "",
) -> str:
    """
    Generate a tight 300-character LinkedIn connection request message.
    Tone varies by contact type:
    - HR/People → mention applying, warm, optional tool mention
    - Data/Analytics/Tech → peer angle, tool mention as conversation starter
    - C-suite large co → ultra-brief, no tool mention
    - Other → professional, optional tool mention
    """
    config = storage.load_config(user_id) if user_id else None

    title_lower = contact_title.lower()
    is_hr = any(t in title_lower for t in [
        "hr", "human resources", "people", "talent", "recruiting", "chro", "chief people"
    ])
    is_data = any(t in title_lower for t in [
        "data", "analytics", "bi ", "intelligence", "cdo", "cto", "technology", "digital"
    ])
    is_csuite = any(t in title_lower for t in [
        "chief executive", "ceo", "president", "co-founder", "founder"
    ])

    if is_hr:
        tone = (
            "HR/People leader. Mention you applied for the role and wanted to connect directly. "
            "Warm and professional. Optionally mention you found their name via an AI job search "
            "tool you built — frame as resourcefulness."
        )
    elif is_data:
        tone = (
            "Data/analytics/tech peer or potential boss. Collegial, peer-to-peer tone. "
            "Definitely mention you built an AI job search tool that identified them as a key "
            "contact — this demonstrates exactly the skills relevant to the role. Make it a "
            "conversation starter."
        )
    elif is_csuite:
        tone = (
            "C-suite executive. Very brief, respectful of their time. "
            "One sentence on who you are, one on the role. Skip the tool mention."
        )
    else:
        tone = (
            "Professional and direct. Mention the role and your background briefly. "
            "Optionally mention the AI tool if it fits naturally."
        )

    first_name = contact_name.split()[0].replace("Dr.", "").strip()
    author_first = author_name.split()[0] if author_name else "Salil"

    system = (
        "You generate LinkedIn connection request messages. "
        "HARD LIMIT: 300 characters total including spaces. Count carefully. "
        "Return ONLY the message text — no quotes, no preamble, no explanation. "
        f"Sign off with just '{author_first}'."
    )

    user_msg = (
        f"Write a LinkedIn connection request from {author_name} to "
        f"{contact_name} ({contact_title}) at {job.company}.\n\n"
        f"Role being applied for: {job.title}\n"
        f"Sender background: Senior data & analytics leader, 30 years experience, "
        f"currently leading federal ERP analytics modernization.\n\n"
        f"Tone: {tone}\n\n"
        f"Open with their first name '{first_name}'. "
        f"Sign off with '{author_first}'. STRICT 300 CHARACTER LIMIT."
    )

    if config:
        return ai_router.chat(system, user_msg, "strong", config, max_tokens=200)
    else:
        # Fallback to direct Anthropic if no config
        return anthropic_chat(system, user_msg, MODEL_STRONG, max_tokens=200)
