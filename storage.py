import json
import os
from pathlib import Path
from models import Job, AppConfig

DATA_DIR = Path("/app/data")
SYSTEM_CONFIG_PATH = DATA_DIR / "system_config.json"


# ── User-scoped directories ────────────────────────────────────────────────────

def get_user_dir(user_id: str) -> Path:
    return DATA_DIR / "users" / user_id


def ensure_user_dirs(user_id: str) -> None:
    base = get_user_dir(user_id)
    for sub in ("jobs", "resumes", "generated", "cover_letters"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def ensure_dirs() -> None:
    """Called at startup — just ensures the top-level data dir exists."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Job persistence ────────────────────────────────────────────────────────────

def save_job(user_id: str, job: Job) -> None:
    ensure_user_dirs(user_id)
    job.user_id = user_id
    path = get_user_dir(user_id) / "jobs" / f"{job.id}.json"
    path.write_text(json.dumps(job.model_dump(), indent=2))


def load_job(user_id: str, job_id: str) -> Job | None:
    path = get_user_dir(user_id) / "jobs" / f"{job_id}.json"
    if not path.exists():
        return None
    return Job(**json.loads(path.read_text()))


def load_all_jobs(user_id: str) -> list[Job]:
    ensure_user_dirs(user_id)
    jobs_dir = get_user_dir(user_id) / "jobs"
    jobs = []
    for f in sorted(jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            jobs.append(Job(**json.loads(f.read_text())))
        except Exception:
            continue
    return jobs


def delete_job(user_id: str, job_id: str) -> bool:
    path = get_user_dir(user_id) / "jobs" / f"{job_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


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
