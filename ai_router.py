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

import logging
import os
import re

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
    "gemini": {
        "fast":   "gemini-2.0-flash",  # 15 RPM, 1500 RPD free tier
        "strong": "gemini-2.5-flash",  # 10 RPM, 500 RPD free tier
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


def _resolve_model(provider: str, tier: str, config) -> str:
    """User override → provider default."""
    field = f"{tier}_model"
    user_model = getattr(config, field, "") or ""
    if user_model.strip():
        return user_model.strip()
    return PROVIDER_DEFAULTS.get(provider, {}).get(tier, "")


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


def _gemini_chat(system: str, user_msg: str, model: str, api_key: str, max_tokens: int) -> str:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    full_prompt = f"{system}\n\n{user_msg}" if system else user_msg
    m = genai.GenerativeModel(model)
    response = m.generate_content(
        full_prompt,
        generation_config=genai.types.GenerationConfig(max_output_tokens=max_tokens),
    )
    return response.text.strip()


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
    url = (config.ollama_url or "http://10.10.10.105:11434").rstrip("/") + "/v1/chat/completions"
    model = config.ollama_model or "gemma4:e4b"
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
        return _gemini_chat(system, user_msg, model, api_key, max_tokens)
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
