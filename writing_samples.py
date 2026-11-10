"""Per-user writing samples, used as voice/style guidance when generating cover letters and tailored resumes.

Layout under users/<user_id>/writing_samples/:
  <sample_id>.txt   extracted plain text
  <sample_id>.json  {"id", "name", "filename", "created_at", "chars"}
  settings.json     {"enabled": bool}
"""

import io
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import storage

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_SAMPLE_CHARS = 20_000       # stored per sample
MAX_SAMPLES = 10
PROMPT_BUDGET_CHARS = 6_000     # total sample text injected into one generation prompt
ALLOWED_EXTENSIONS = (".docx", ".pdf", ".txt", ".md")

_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _dir(user_id: str) -> Path:
    d = storage.get_user_dir(user_id) / "writing_samples"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _paths(user_id: str, sample_id: str) -> tuple[Path, Path]:
    if not _ID_RE.match(sample_id or ""):
        raise KeyError(sample_id)
    d = _dir(user_id)
    return d / f"{sample_id}.txt", d / f"{sample_id}.json"


def extract_text(data: bytes, filename: str) -> str:
    """Plain text from an uploaded file. Raises ValueError for unsupported or unreadable files."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported file type; use " + ", ".join(ALLOWED_EXTENSIONS))
    try:
        if ext == ".docx":
            from docx import Document
            doc = Document(io.BytesIO(data))
            return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"Could not read {ext} file: {e}")


def _clean(text: str) -> str:
    text = storage.sanitize_unicode(text or "").replace("\r\n", "\n").replace("\x00", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def list_samples(user_id: str) -> list[dict]:
    out = []
    for meta_path in sorted(_dir(user_id).glob("*.json")):
        if meta_path.name == "settings.json":
            continue
        try:
            out.append(json.loads(meta_path.read_text()))
        except Exception:
            continue
    out.sort(key=lambda m: m.get("created_at", ""))
    return out


def add_sample(user_id: str, name: str, text: str, filename: str = "") -> dict:
    text = _clean(text)
    if not text:
        raise ValueError("No text found in that sample")
    if len(list_samples(user_id)) >= MAX_SAMPLES:
        raise ValueError(f"Sample limit reached ({MAX_SAMPLES}); delete one first")
    truncated = len(text) > MAX_SAMPLE_CHARS
    text = text[:MAX_SAMPLE_CHARS]
    sample_id = secrets.token_hex(6)
    txt_path, meta_path = _paths(user_id, sample_id)
    name = (name or "").strip() or Path(filename).stem or "Untitled sample"
    meta = {
        "id": sample_id, "name": name[:120], "filename": Path(filename).name if filename else "",
        "created_at": datetime.now(timezone.utc).isoformat(), "chars": len(text), "truncated": truncated,
    }
    txt_path.write_text(text, encoding="utf-8")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta


def get_text(user_id: str, sample_id: str) -> str | None:
    try:
        txt_path, _ = _paths(user_id, sample_id)
    except KeyError:
        return None
    return txt_path.read_text(encoding="utf-8") if txt_path.exists() else None


def delete_sample(user_id: str, sample_id: str) -> bool:
    try:
        txt_path, meta_path = _paths(user_id, sample_id)
    except KeyError:
        return False
    existed = txt_path.exists() or meta_path.exists()
    txt_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return existed


def is_enabled(user_id: str) -> bool:
    path = _dir(user_id) / "settings.json"
    try:
        return bool(json.loads(path.read_text()).get("enabled", True))
    except Exception:
        return True


def set_enabled(user_id: str, enabled: bool) -> None:
    (_dir(user_id) / "settings.json").write_text(json.dumps({"enabled": bool(enabled)}))


def prompt_text(user_id: str | None, budget: int = PROMPT_BUDGET_CHARS) -> str:
    """The sample text to put in a prompt: oldest first, split evenly, capped at `budget` chars total.
    Empty when the user has no samples or has turned the feature off."""
    if not user_id or not is_enabled(user_id):
        return ""
    texts = [t for t in (get_text(user_id, m["id"]) for m in list_samples(user_id)) if t]
    if not texts:
        return ""
    share = max(500, budget // len(texts))
    parts, used = [], 0
    for t in texts:
        piece = t[:min(share, budget - used)]
        if not piece:
            break
        parts.append(piece)
        used += len(piece)
    return "\n\n---\n\n".join(parts)


COVER_LETTER_GUIDANCE = (
    "\n\nCANDIDATE WRITING SAMPLES (the candidate's own writing). Match their voice: sentence length and rhythm, "
    "formality, word choice, how they open and close. Use these for style only. Do not copy sentences, and do not "
    "take facts, employers, or accomplishments from them; facts come only from the resume above.\n\n{samples}\n"
)

RESUME_GUIDANCE = (
    "\n\nCANDIDATE WRITING SAMPLES (the candidate's own writing). Where you rewrite the Summary or bullet wording, "
    "keep the candidate's natural voice and phrasing habits. This is style guidance only: never take facts, "
    "metrics, or claims from these samples, and every rule above about accuracy still applies.\n\n{samples}\n"
)


def with_cover_letter_guidance(system: str, user_id: str | None) -> str:
    samples = prompt_text(user_id)
    return system + COVER_LETTER_GUIDANCE.replace("{samples}", samples) if samples else system


def with_resume_guidance(system: str, user_id: str | None) -> str:
    samples = prompt_text(user_id)
    return system + RESUME_GUIDANCE.replace("{samples}", samples) if samples else system
