"""Resume lanes: which uploaded resume a job should use.

A lane is one of the user's resume slots (director / base / contract). The scorer classifies a job
into a lane by comparing the job to each lane's description; the user can override it per job.
Everything that needs "the resume for this job" (tailoring, cover letters, the agent API that
JobPilot calls) goes through resolve(), so there is one answer.

Market lanes on a job (w2_sniper / contract / contract_to_hire / ignore) are unchanged: they describe
employment type and score band, not which resume to send.
"""
import storage
from models import MarketLane

LANE_KEYS = ("director", "base", "contract")

DEFAULT_DESCRIPTIONS = {
    "director": "Leadership and director-level roles: managing teams and programs, architecture and strategy, stakeholder-facing.",
    "base": "General-purpose roles that mix hands-on work with some leadership, or don't clearly fit the other lanes.",
    "contract": "Contract and hands-on roles where a keyword-dense, quick-to-skim resume wins: specific tools and delivered outcomes.",
}

# Used only when a job has no valid scored lane (e.g. scored before lanes existed, or the model's answer was unusable).
_FROM_MARKET_LANE = {
    MarketLane.W2_SNIPER: "director",
    MarketLane.CONTRACT: "contract",
    MarketLane.CONTRACT_TO_HIRE: "base",
}
_FALLBACK_ORDER = ("base", "director", "contract")


def label(config, key: str) -> str:
    return (config.resume_labels or {}).get(key) or key


def description(config, key: str) -> str:
    return ((config.resume_lane_descriptions or {}).get(key) or "").strip() or DEFAULT_DESCRIPTIONS[key]


def loaded_keys(user_id: str) -> list[str]:
    return [k for k in LANE_KEYS if storage.load_resume_text(user_id, k)]


def available(user_id: str, config) -> list[dict]:
    """The lanes that have an uploaded resume, with the text the scorer sees."""
    return [{"key": k, "label": label(config, k), "description": description(config, k)}
            for k in loaded_keys(user_id)]


def prompt_block(lanes: list[dict]) -> str:
    if not lanes:
        return 'RESUME LANE: no resumes are uploaded, so set "recommended_resume" to "none".'
    rows = "\n".join(f'- "{l["key"]}" ({l["label"]}): {l["description"]}' for l in lanes)
    return (
        "RESUME LANE: choose the ONE uploaded resume whose lane best fits this role, judging by the job's "
        "responsibilities and level, not by whether it is W2 or contract. Lanes:\n"
        f"{rows}\n"
        'Set "recommended_resume" to that lane key. Use "none" only when the lane assignment above is "ignore".'
    )


def keys_for_schema(lanes: list[dict]) -> str:
    return "|".join([l["key"] for l in lanes] + ["none"])


def coerce(value, lanes: list[dict], market_lane) -> str:
    """The scorer's answer if it names an available lane, else the market-lane default if available."""
    keys = [l["key"] for l in lanes]
    v = str(value or "").strip().lower()
    if v in keys:
        return v
    if v == "none" and market_lane == MarketLane.IGNORE:
        return "none"
    fallback = _FROM_MARKET_LANE.get(market_lane)
    if fallback in keys:
        return fallback
    return "none" if market_lane == MarketLane.IGNORE else (keys[0] if keys else "none")


def resolve(job, user_id: str, loaded: list[str] | None = None, use_override: bool = True) -> tuple[str, str]:
    """(lane key, source) for a job. source: manual | scored | default | none. Key is "" for none.

    Pass `loaded` (from loaded_keys) when resolving many jobs; use_override=False gives the automatic lane.
    """
    override = getattr(job, "resume_lane_override", "") or ""
    if use_override and override in LANE_KEYS:
        return override, "manual"
    if loaded is None:
        loaded = loaded_keys(user_id)
    scored = (job.score.recommended_resume if job.score else "") or ""
    if scored in loaded:
        return scored, "scored"
    mapped = _FROM_MARKET_LANE.get(job.market_lane)
    if mapped in loaded:
        return mapped, "default"
    for k in _FALLBACK_ORDER:
        if k in loaded:
            return k, "default"
    return "", "none"
