import json
import os
import re
from pathlib import Path
from models import Job, AppConfig

DATA_DIR = Path(os.environ.get("JSHQ_DATA_DIR", "/app/data"))
SYSTEM_CONFIG_PATH = DATA_DIR / "system_config.json"

_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def sanitize_unicode(value):
    """Strip unpaired UTF-16 surrogates from strings (recursively through
    dicts/lists). A valid surrogate pair from JSON (\\uD83D\\uDE00 etc.) is
    already combined into one real code point by the time json.loads hands
    it to us, so any surrogate code point left in a Python str is by
    definition unpaired/lone — safe to drop outright. Left uncaught, these
    make it into a saved job and then blow up UnicodeEncodeError the next
    time that string is encoded to UTF-8 (e.g. serializing an API response),
    which is what happened with a truncated LinkedIn JD containing styled
    Unicode text."""
    if isinstance(value, str):
        return _LONE_SURROGATE_RE.sub("", value)
    if isinstance(value, dict):
        return {k: sanitize_unicode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_unicode(v) for v in value]
    return value


# ── User-scoped directories ────────────────────────────────────────────────────

def get_user_dir(user_id: str) -> Path:
    return DATA_DIR / "users" / user_id


def ensure_user_dirs(user_id: str) -> None:
    base = get_user_dir(user_id)
    for sub in ("jobs", "jobs_archive", "resumes", "generated", "cover_letters"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def get_archive_dir(user_id: str) -> Path:
    d = get_user_dir(user_id) / "jobs_archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dirs() -> None:
    """Called at startup — just ensures the top-level data dir exists."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Job persistence ────────────────────────────────────────────────────────────
#
# Active jobs live in jobs/, one file per job. Once a job is stale (old +
# terminal/low-scoring — see archiving.py), it's moved to jobs_archive/ so the
# hot paths (tracker view, dedup checks) stop paying to load and parse it.
# Archived jobs are never deleted outright: a small fingerprint of every job
# ever seen (active or archived) lives in the dedup index below, so scrapers
# never re-import something we already passed on.

def save_job(user_id: str, job: Job) -> None:
    ensure_user_dirs(user_id)
    job.user_id = user_id
    data = sanitize_unicode(job.model_dump())
    for field, value in data.items():
        setattr(job, field, value)
    path = get_user_dir(user_id) / "jobs" / f"{job.id}.json"
    path.write_text(json.dumps(data, indent=2))
    update_dedup_index(user_id, job, archived=False)


def load_job(user_id: str, job_id: str) -> Job | None:
    path = get_user_dir(user_id) / "jobs" / f"{job_id}.json"
    if not path.exists():
        path = get_archive_dir(user_id) / f"{job_id}.json"
    if not path.exists():
        return None
    return Job(**json.loads(path.read_text()))


def load_all_jobs(user_id: str, include_archived: bool = False) -> list[Job]:
    """Load active jobs (the tracker/dashboard hot path). Pass include_archived=True
    only for views that specifically need archived jobs too (e.g. an Archive tab) —
    the whole point of archiving is to keep this off the common path."""
    ensure_user_dirs(user_id)
    dirs = [get_user_dir(user_id) / "jobs"]
    if include_archived:
        dirs.append(get_archive_dir(user_id))
    entries = []
    for d in dirs:
        for f in d.glob("*.json"):
            entries.append(f)
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    jobs = []
    for f in entries:
        try:
            jobs.append(Job(**json.loads(f.read_text())))
        except Exception:
            continue
    return jobs


def load_archived_jobs(user_id: str) -> list[Job]:
    d = get_archive_dir(user_id)
    jobs = []
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            jobs.append(Job(**json.loads(f.read_text())))
        except Exception:
            continue
    return jobs


def archive_job(user_id: str, job_id: str) -> bool:
    """Move an active job to jobs_archive/. Keeps its dedup fingerprint."""
    src = get_user_dir(user_id) / "jobs" / f"{job_id}.json"
    if not src.exists():
        return False
    dst = get_archive_dir(user_id) / f"{job_id}.json"
    src.rename(dst)
    try:
        job = Job(**json.loads(dst.read_text()))
        update_dedup_index(user_id, job, archived=True)
    except Exception:
        pass
    return True


def unarchive_job(user_id: str, job_id: str) -> bool:
    src = get_archive_dir(user_id) / f"{job_id}.json"
    if not src.exists():
        return False
    dst = get_user_dir(user_id) / "jobs" / f"{job_id}.json"
    src.rename(dst)
    try:
        job = Job(**json.loads(dst.read_text()))
        update_dedup_index(user_id, job, archived=False)
    except Exception:
        pass
    return True


def delete_job(user_id: str, job_id: str) -> bool:
    for base in (get_user_dir(user_id) / "jobs", get_archive_dir(user_id)):
        path = base / f"{job_id}.json"
        if path.exists():
            path.unlink()
            return True
    return False


# ── Dedup index ─────────────────────────────────────────────────────────────────
#
# A single small JSON file of {id, url_key, title_key, archived} per job ever
# seen. Scrapers and email/LinkedIn intake match against this instead of
# loading every full job record (which carries JD text, cover letters, agent
# logs, etc.) — the difference is what keeps a search with 2500+ jobs on file
# from opening 2500 files just to answer "have we seen this one?"

def _dedup_index_path(user_id: str) -> Path:
    return get_user_dir(user_id) / "dedup_index.json"


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _normalize_title_company(title: str, company: str) -> str:
    return f"{(title or '').strip().lower()}|{(company or '').strip().lower()}"


def dedup_entry(job: Job, archived: bool = False) -> dict:
    return {
        "id": job.id,
        "url_key": _normalize_url(job.url) if job.url else "",
        "title_key": _normalize_title_company(job.title, job.company),
        "archived": archived,
    }


def load_dedup_index(user_id: str) -> list[dict]:
    path = _dedup_index_path(user_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save_dedup_index(user_id: str, index: list[dict]) -> None:
    ensure_user_dirs(user_id)
    _dedup_index_path(user_id).write_text(json.dumps(index, indent=2))


def update_dedup_index(user_id: str, job: Job, archived: bool = False) -> None:
    index = load_dedup_index(user_id)
    index = [e for e in index if e.get("id") != job.id]
    index.append(dedup_entry(job, archived=archived))
    _save_dedup_index(user_id, index)


def find_in_dedup_index(index: list[dict], title: str, company: str, url: str) -> dict | None:
    """Match a candidate scrape result against a preloaded dedup index (by URL first,
    then title+company). Callers loop many candidates per index load, so the index
    is passed in rather than reloaded per call."""
    url_key = _normalize_url(url) if url else ""
    title_key = _normalize_title_company(title, company)
    for e in index:
        if url_key and e.get("url_key") == url_key:
            return e
        if e.get("title_key") == title_key:
            return e
    return None


# ── Per-user config ────────────────────────────────────────────────────────────

def load_config(user_id: str) -> AppConfig:
    path = get_user_dir(user_id) / "config.json"
    if path.exists():
        try:
            return AppConfig(**json.loads(path.read_text()))
        except Exception:
            pass
    config = AppConfig()
    save_config(user_id, config)
    return config


def save_config(user_id: str, config: AppConfig) -> None:
    ensure_user_dirs(user_id)
    path = get_user_dir(user_id) / "config.json"
    path.write_text(json.dumps(config.model_dump(), indent=2))


# ── Per-user resumes ───────────────────────────────────────────────────────────

def load_resume_text(user_id: str, variant: str) -> str:
    path = get_user_dir(user_id) / "resumes" / f"{variant}.txt"
    if path.exists():
        return path.read_text()
    return ""


def save_resume_text(user_id: str, variant: str, content: str) -> None:
    ensure_user_dirs(user_id)
    path = get_user_dir(user_id) / "resumes" / f"{variant}.txt"
    path.write_text(content)


# ── Generated document paths ───────────────────────────────────────────────────

def get_generated_dir(user_id: str) -> Path:
    d = get_user_dir(user_id) / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_cover_letter_dir(user_id: str, job_id: str) -> Path:
    d = get_user_dir(user_id) / "cover_letters" / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_cover_letter_files(user_id: str, job_id: str) -> list[dict]:
    d = get_user_dir(user_id) / "cover_letters" / job_id
    if not d.exists():
        return []
    return [
        {"filename": f.name, "path": str(f), "size": f.stat().st_size}
        for f in sorted(d.glob("*.docx"))
    ]


# ── System config (OIDC, session secret) ──────────────────────────────────────
# Thin wrappers — auth.py owns the full implementation;
# these exist so other modules can import from storage without circular imports.

def load_system_config() -> dict:
    if SYSTEM_CONFIG_PATH.exists():
        try:
            return json.loads(SYSTEM_CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_system_config(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEM_CONFIG_PATH.write_text(json.dumps(config, indent=2))
