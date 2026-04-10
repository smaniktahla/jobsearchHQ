import json
import os
from pathlib import Path
from models import Job, AppConfig

DATA_DIR = Path("/app/data")
JOBS_DIR = DATA_DIR / "jobs"
CONFIG_PATH = DATA_DIR / "config.json"


def ensure_dirs():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "resumes").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "cover_letters").mkdir(parents=True, exist_ok=True)


def save_job(job: Job):
    ensure_dirs()
    path = JOBS_DIR / f"{job.id}.json"
    path.write_text(json.dumps(job.model_dump(), indent=2))


def load_job(job_id: str) -> Job | None:
    path = JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        return None
    return Job(**json.loads(path.read_text()))


def load_all_jobs() -> list[Job]:
    ensure_dirs()
    jobs = []
    for f in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            jobs.append(Job(**json.loads(f.read_text())))
        except Exception:
            continue
    return jobs


def delete_job(job_id: str) -> bool:
    path = JOBS_DIR / f"{job_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        return AppConfig(**json.loads(CONFIG_PATH.read_text()))
    config = AppConfig()
    save_config(config)
    return config


def save_config(config: AppConfig):
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(config.model_dump(), indent=2))


def load_resume_text(variant: str) -> str:
    """Load resume text for scoring context. Variant: 'director', 'base', 'contract', 'full_history'"""
    path = DATA_DIR / "resumes" / f"{variant}.txt"
    if path.exists():
        return path.read_text()
    return ""


def save_resume_text(variant: str, content: str):
    ensure_dirs()
    path = DATA_DIR / "resumes" / f"{variant}.txt"
    path.write_text(content)


def get_resume_docx_path(variant: str) -> Path:
    """Get path to a resume .docx file."""
    return DATA_DIR / "resumes" / f"{variant}.docx"


def resume_docx_exists(variant: str) -> bool:
    return get_resume_docx_path(variant).exists()


def get_cover_letter_dir(job_id: str) -> Path:
    d = DATA_DIR / "cover_letters" / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_cover_letter_files(job_id: str) -> list[dict]:
    d = DATA_DIR / "cover_letters" / job_id
    if not d.exists():
        return []
    files = []
    for f in sorted(d.glob("*.docx")):
        files.append({"filename": f.name, "path": str(f), "size": f.stat().st_size})
    return files
