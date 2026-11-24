"""
AI Router for Job Search HQ.

Unified LLM interface. All modules call chat() — never talk to providers directly.

Supported providers: anthropic | gemini | openai | ollama

Split provider support:
  config.fast_provider  — scoring, metadata, research (set to Gemini for free tier)
  config.strong_provider — resumes, cover letters, LinkedIn messages (set to Anthropic for quality)

API key resolution (per call):
  1. User config field (anthropic_api_key / gemini_api_key / openai_api_key)
  2. System environment variable (ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY)
  3. Raise ValueError if neither found

web_search_chat() always uses Anthropic — Claude's server_tool_use web_search
is not available on other providers.
"""

import contextvars
import hashlib
import json
import time
import urllib.error
import urllib.request
import logging
import os
import re
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ── Last model used (for UI display) ─────────────────────────────────────────
_last_model_used: dict = {"fast": "", "strong": ""}

def get_last_model(tier: str) -> str:
    """Return the provider/model used for the last call of the given tier."""
    return _last_model_used.get(tier, "")

# ── Default models per provider ────────────────────────────────────────────────

PROVIDER_DEFAULTS = {
    "anthropic": {
        "fast":   "claude-haiku-4-5-20251001",
        "strong": "claude-sonnet-4-5",
    },
    # Gemini defaults are only the offline fallback: normally the newest stable
    # model is discovered from the API (see _discover_gemini_models). Pin a model
    # in Settings -> AI to override.
    "gemini": {
        "fast":   "gemini-flash-lite-latest",  # Google alias -> newest Flash-Lite
        "strong": "gemini-flash-latest",       # Google alias -> newest Flash
    },
    "openai": {
        "fast":   "gpt-4o-mini",
        "strong": "gpt-4o",
    },
    "ollama": {
        "fast":   "",   # uses config.ollama_model
        "strong": "",
    },
}

ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "openai":    "OPENAI_API_KEY",
}


# ── Key & model resolution ─────────────────────────────────────────────────────

class ProviderConfigError(ValueError):
    """The selected AI provider isn't usable as configured (missing key/URL).
    Callers surface this as a 400 with a fix-it message, not a 500."""


TIER_LABELS = {"fast": "Fast", "strong": "Strong"}

def _resolve_key(provider: str, config) -> str:
    """User config key → env var → raise."""
    field = f"{provider}_api_key"
    user_key = getattr(config, field, "") or ""
    if user_key.strip():
        return user_key.strip()
    env_var = ENV_KEY_MAP.get(provider, "")
    env_key = os.environ.get(env_var, "") if env_var else ""
    if env_key.strip():
        return env_key.strip()
    raise ProviderConfigError(
        f"No API key for '{provider}'. Add it in Settings → AI, "
        f"or set the {env_var or provider.upper() + '_API_KEY'} environment variable."
    )


def validate_provider(tier: str, config) -> str:
    """Pre-flight check for a tier's provider. Returns the provider name, or
    raises ProviderConfigError naming the tier and the exact setting to fix —
    so a misconfigured provider fails fast and legibly before any prompt is built."""
    field = "fast_provider" if tier == "fast" else "strong_provider"
    provider = getattr(config, field, "anthropic") or "anthropic"
    label = TIER_LABELS.get(tier, tier)
    if provider == "ollama":
        if not (getattr(config, "ollama_url", "") or "").strip():
            raise ProviderConfigError(
                f"{label} provider is Local (Ollama/Hermes) but no URL is set. Add one in Settings → AI."
            )
        return provider
    if provider not in ENV_KEY_MAP:
        raise ProviderConfigError(f"{label} provider '{provider}' is not recognized. Pick one in Settings → AI.")
    try:
        _resolve_key(provider, config)
    except ProviderConfigError as e:
        raise ProviderConfigError(
            f"{label} provider is set to {provider} but isn't usable: {e} "
            f"Or switch the {label} provider to another option in Settings → AI."
        ) from None
    return provider


_GEMINI_CACHE_TTL = 6 * 3600
_GEMINI_BAD_TTL = 600            # seconds a failing model is skipped
_gemini_bad: dict = {}           # model name -> time of last failure
_gemini_model_cache: dict = {}   # sha1(api_key) -> (fetched_at, {"fast": name, "strong": name})
# Stable text models only: gemini-<ver>-flash / gemini-<ver>-flash-lite. The
# anchored pattern excludes -preview, -tts, -image, -live, -audio, omni, etc.
_GEMINI_FLASH_RE = re.compile(r"^models/(gemini-(\d+(?:\.\d+)?)-flash(-lite)?)$")


def _discover_gemini_models(api_key: str) -> dict:
    """Ask the API which models this key can use; return the newest stable
    Flash (strong tier) and Flash-Lite (fast tier). Cached per key for 6h.
    Returns {} if discovery fails, so callers fall back to the *-latest aliases."""
    ck = hashlib.sha1(api_key.encode()).hexdigest()
    hit = _gemini_model_cache.get(ck)
    if hit and time.time() - hit[0] < _GEMINI_CACHE_TTL:
        return hit[1]
    best: dict = {"fast": [], "strong": []}
    try:
        token = ""
        for _ in range(5):  # page through the listing
            url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"
            if token:
                url += f"&pageToken={token}"
            req = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
            data = json.load(urllib.request.urlopen(req, timeout=10))
            for m in data.get("models", []):
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                mt = _GEMINI_FLASH_RE.match(m.get("name", ""))
                if not mt:
                    continue
                ver = tuple(int(x) for x in mt.group(2).split("."))
                tier = "fast" if mt.group(3) else "strong"
                best[tier].append((ver, mt.group(1)))
            token = data.get("nextPageToken", "")
            if not token:
                break
    except Exception as e:
        logger.warning(f"[ai_router] Gemini model discovery failed ({type(e).__name__}); using -latest aliases")
        # Retry in 5 min, not on every call
        _gemini_model_cache[ck] = (time.time() - _GEMINI_CACHE_TTL + 300, {})
        return {}
    found = {t: [n for _, n in sorted(set(v), reverse=True)] for t, v in best.items()}
    _gemini_model_cache[ck] = (time.time(), found)
    logger.info(f"[ai_router] Gemini models discovered: {found}")
    return found


def _resolve_model(provider: str, tier: str, config) -> str:
    """User override -> (Gemini: newest model the key offers) -> provider default."""
    field = f"{tier}_model"
    user_model = getattr(config, field, "") or ""
    if user_model.strip():
        return user_model.strip()
    if provider == "gemini":
        try:
            found = _discover_gemini_models(_resolve_key("gemini", config))
        except ProviderConfigError:
            found = {}
        if found.get(tier):
            return found[tier][0]
    return PROVIDER_DEFAULTS.get(provider, {}).get(tier, "")


def _gemini_candidates(tier: str, config) -> list:
    """Models to try for a Gemini call, best first. A model pinned in Settings is
    used alone (never silently swapped). Otherwise: the newest stable models the
    key offers (top 3), then the Google -latest alias as a last resort."""
    pinned = (getattr(config, f"{tier}_model", "") or "").strip()
    if pinned:
        return [pinned]
    try:
        found = _discover_gemini_models(_resolve_key("gemini", config)).get(tier, [])
    except ProviderConfigError:
        found = []
    out = list(found[:5])
    fb = PROVIDER_DEFAULTS["gemini"][tier]
    if fb not in out:
        out.append(fb)
    return out


def _gemini_chat_with_fallback(system, user_msg, tier, config, api_key, max_tokens):
    """Try each candidate with a bounded timeout; move on if a model is slow,
    unavailable, retired, rate-limited or returns no text."""
    cands = _gemini_candidates(tier, config)
    # Skip models that failed recently (new models are often overloaded on the
    # free tier) so later calls don't pay the timeout again; if every candidate
    # is cooling down, try them all anyway.
    now = time.time()
    live = [m for m in cands if now - _gemini_bad.get(m, 0) > _GEMINI_BAD_TTL]
    if len(cands) > 1 and live:
        cands = live
    timeout = 12 if max_tokens <= 1000 else 120   # small calls should answer fast
    last = None
    for i, model in enumerate(cands):
        try:
            out = _gemini_chat(system, user_msg, model, api_key, max_tokens, timeout=timeout)
            _last_model_used[tier] = f"gemini/{model}"
            return out
        except Exception as e:
            last = e
            if len(cands) > 1:
                _gemini_bad[model] = time.time()
            logger.warning(f"[ai_router] gemini/{model} failed ({type(e).__name__}: {str(e)[:120]}); "
                           + ("trying next" if i + 1 < len(cands) else "no more candidates"))
    raise last


# ── Model listing (for the model pickers in the wizard / Settings) ────────────

_GEMINI_SKIP = re.compile(r"tts|image|embed|aqa|imagen|veo|live|audio|robotics|computer-use|learnlm|omni|lyria", re.I)
_OPENAI_SKIP = re.compile(r"audio|realtime|image|tts|transcribe|embed|moderation|search|whisper|dall|sora|instruct|davinci|babbage", re.I)


def _http_json(url: str, headers: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise ProviderConfigError("The provider rejected this API key. Double-check it and try again.") from None
        raise ProviderConfigError(f"The provider returned an error ({e.code}). Try again in a minute.") from None
    except Exception as e:
        raise ProviderConfigError(f"Could not reach the provider ({type(e).__name__}).") from None


def list_models(provider: str, api_key: str = "", base_url: str = "") -> dict:
    """Models a key can use, for pickers. Returns
    {"models": [{"id","label"}], "recommended": {"fast": id, "strong": id}}.
    Raises ProviderConfigError with a user-readable message."""
    models, rec = [], {}
    if provider == "gemini":
        names, token = [], ""
        for _ in range(5):
            url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200" + (f"&pageToken={token}" if token else "")
            data = _http_json(url, {"x-goog-api-key": api_key})
            for m in data.get("models", []):
                nm = m.get("name", "").replace("models/", "")
                if "generateContent" in m.get("supportedGenerationMethods", []) and nm.startswith("gemini") \
                        and not _GEMINI_SKIP.search(nm):
                    names.append((nm, m.get("displayName") or nm))
            token = data.get("nextPageToken", "")
            if not token:
                break
        models = [{"id": n, "label": l} for n, l in sorted(set(names), reverse=True)]
        found = _discover_gemini_models(api_key)
        rec = {t: (found.get(t) or [""])[0] for t in ("fast", "strong")}
    elif provider == "anthropic":
        data = _http_json("https://api.anthropic.com/v1/models?limit=100",
                          {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        models = [{"id": m["id"], "label": m.get("display_name") or m["id"]} for m in data.get("data", [])]
        rec = dict(PROVIDER_DEFAULTS["anthropic"])
    elif provider == "openai":
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        data = _http_json(base + "/models", {"Authorization": f"Bearer {api_key}"})
        ids = sorted({m["id"] for m in data.get("data", [])
                      if re.match(r"^(gpt|o\d|chatgpt)", m["id"]) and not _OPENAI_SKIP.search(m["id"])}, reverse=True)
        models = [{"id": i, "label": i} for i in ids]
        rec = dict(PROVIDER_DEFAULTS["openai"])
    elif provider == "ollama":
        base = (base_url or "").rstrip("/")
        if not base:
            raise ProviderConfigError("Enter the server URL first.")
        data = _http_json(base + "/api/tags", {})
        models = [{"id": m["name"], "label": m["name"]} for m in data.get("models", [])]
    else:
        raise ProviderConfigError("Unknown provider.")
    return {"models": models, "recommended": rec}


# ── Provider implementations ───────────────────────────────────────────────────

def _anthropic_chat(system: str, user_msg: str, model: str, api_key: str, max_tokens: int) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return msg.content[0].text.strip()


def _gemini_chat(system: str, user_msg: str, model: str, api_key: str, max_tokens: int, timeout: int = 120) -> str:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    full_prompt = f"{system}\n\n{user_msg}" if system else user_msg
    m = genai.GenerativeModel(model)
    response = m.generate_content(
        full_prompt,
        generation_config=genai.types.GenerationConfig(max_output_tokens=max_tokens),
        request_options={"timeout": timeout, "retry": None},
    )
    try:
        text = response.text.strip()
    except ValueError:
        raise ValueError(f"{model} returned no text (finish_reason="
                         f"{response.candidates[0].finish_reason if response.candidates else 'none'}); "
                         f"likely the token limit was consumed by model reasoning")
    # A reasoning model can spend the whole budget thinking and return a few lines of half a document.
    # Treat that as a failure so the next model is tried, instead of saving a truncated resume.
    if response.candidates and "MAX_TOKENS" in str(response.candidates[0].finish_reason).upper():
        raise ValueError(f"{model} hit the token limit mid-answer ({len(text)} chars returned)")
    return text


def _openai_chat(system: str, user_msg: str, model: str, api_key: str, max_tokens: int, base_url: str = "") -> str:
    from openai import OpenAI
    # base_url lets this provider talk to any OpenAI-compatible server
    # (llama-server, vLLM, LM Studio) instead of api.openai.com.
    client = OpenAI(api_key=api_key, base_url=base_url or None)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_msg})
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    return resp.choices[0].message.content.strip()


def _ollama_chat(system: str, user_msg: str, config, json_mode: bool = False, max_tokens: int = 4000, timeout: int = 120) -> str:
    import requests as req
    # OpenAI-compatible endpoint — works against both real Ollama and a bare
    # llama.cpp llama-server (which only speaks this protocol, not Ollama's
    # native /api/chat). chat_template_kwargs.enable_thinking=False suppresses
    # hidden reasoning tokens on templates that support it (e.g. Gemma4); it's
    # a no-op on servers/templates that don't recognize the field.
    if not (config.ollama_url or "").strip() or not (config.ollama_model or "").strip():
        raise ProviderConfigError("Local server URL and model name are required. Add them in Settings → AI.")
    url = config.ollama_url.rstrip("/") + "/v1/chat/completions"
    model = config.ollama_model
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_msg})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if json_mode:
        # Grammar-constrained decoding — guarantees syntactically valid JSON,
        # instead of relying on the prompt alone (which local models occasionally
        # violate: unescaped quotes, stray control chars, missing commas).
        payload["response_format"] = {"type": "json_object"}
    r = req.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── Batch routing ──────────────────────────────────────────────────────────────

_in_batch: contextvars.ContextVar[bool] = contextvars.ContextVar("ai_router_batch", default=False)


@contextmanager
def batch_mode():
    """Mark LLM calls made inside (or in a function decorated with this) as large slow batch work.

    When the user's config sets batch_ollama_url, those calls go to that local server instead of
    the configured fast/strong provider. Context-local, so set it inside the thread doing the work.
    """
    token = _in_batch.set(True)
    try:
        yield
    finally:
        _in_batch.reset(token)


class _BatchTarget:
    """Config view that points the Ollama call at the batch server."""
    def __init__(self, config):
        self.ollama_url = config.batch_ollama_url
        self.ollama_model = config.batch_ollama_model or config.ollama_model


# ── Public interface ───────────────────────────────────────────────────────────

def chat(
    system: str,
    user_msg: str,
    tier: str,       # "fast" or "strong"
    config,          # AppConfig instance
    max_tokens: int = 4000,
    json_mode: bool = False,
) -> str:
    """
    Route an LLM call to the configured provider for this tier.
    tier="fast"   → config.fast_provider   (scoring, metadata, research)
    tier="strong" → config.strong_provider (resumes, cover letters, messages)

    json_mode=True constrains the response to valid JSON where the provider
    supports it (currently: Ollama). Only pass this when the caller actually
    parses the reply as JSON — it changes the wire format, not just a hint.
    """
    if _in_batch.get() and (getattr(config, "batch_ollama_url", "") or "").strip():
        target = _BatchTarget(config)
        logger.info(f"[ai_router] batch -> {target.ollama_url} {target.ollama_model} | tier={tier}")
        _last_model_used[tier] = f"ollama/{target.ollama_model} (batch)"
        # The batch server is typically a busier or slower box: allow a longer budget than interactive.
        return _ollama_chat(system, user_msg, target, json_mode=json_mode, max_tokens=max_tokens,
                            timeout=600 if tier == "strong" else 240)

    provider = validate_provider(tier, config)

    model = _resolve_model(provider, tier, config)
    logger.info(f"[ai_router] {provider}/{model} | tier={tier}")
    _last_model_used[tier] = f"{provider}/{model}"

    if provider == "ollama":
        _last_model_used[tier] = f"ollama/{getattr(config, 'ollama_model', 'unknown')}"
        # Strong-tier jobs (full resume = ~4k tokens) outlast the 120s fast-tier budget on local models.
        return _ollama_chat(system, user_msg, config, json_mode=json_mode, max_tokens=max_tokens,
                            timeout=300 if tier == "strong" else 120)

    api_key = _resolve_key(provider, config)

    if provider == "anthropic":
        return _anthropic_chat(system, user_msg, model, api_key, max_tokens)
    elif provider == "gemini":
        return _gemini_chat_with_fallback(system, user_msg, tier, config, api_key, max_tokens)
    elif provider == "openai":
        return _openai_chat(system, user_msg, model, api_key, max_tokens,
                            base_url=(getattr(config, "openai_base_url", "") or "").strip())
    else:
        raise ValueError(f"Unknown provider: '{provider}'")


def web_search_chat(prompt: str, max_tokens: int = 1000) -> str:
    """
    Claude web search via server_tool_use. Always Anthropic — this tool
    is Claude-only. Uses system ANTHROPIC_API_KEY (not user key).
    """
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY env var required for web search (company research)")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    text_blocks = [b.text.strip() for b in response.content if b.type == "text" and b.text.strip()]
    return text_blocks[-1] if text_blocks else ""
