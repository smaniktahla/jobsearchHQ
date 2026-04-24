#!/usr/bin/env python3
"""
Migrate v1 (single-user flat layout) → v2 (multi-user layout).

Usage:
  python migrate.py <user_sub>

Where <user_sub> is the OIDC 'sub' claim for the existing user.
Find it by logging into v2 and visiting /api/auth/me.

This script is safe to run multiple times (already-migrated files are skipped).
"""
import json
import shutil
import sys
from pathlib import Path

V1_DATA = Path("/app/data")       # or override below for local runs
V2_DATA = Path("/app/data")       # same root in v2 (users/ subdir is new)


def migrate(user_sub: str):
    user_dir = V2_DATA / "users" / user_sub
    for sub in ("jobs", "resumes", "generated", "cover_letters"):
        (user_dir / sub).mkdir(parents=True, exist_ok=True)

    # Jobs
    jobs_src = V1_DATA / "jobs"
    if jobs_src.exists():
        moved = 0
        for f in jobs_src.glob("*.json"):
            dest = user_dir / "jobs" / f.name
            if dest.exists():
                continue
            try:
                data = json.loads(f.read_text())
                data["user_id"] = user_sub      # stamp ownership
                dest.write_text(json.dumps(data, indent=2))
                moved += 1
            except Exception as e:
                print(f"  WARN: could not migrate {f.name}: {e}")
        print(f"Jobs: migrated {moved} files")
    else:
        print("Jobs: no v1 jobs/ directory found")

    # Resumes
    resumes_src = V1_DATA / "resumes"
    if resumes_src.exists():
        moved = 0
        for f in resumes_src.iterdir():
            dest = user_dir / "resumes" / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                moved += 1
        print(f"Resumes: migrated {moved} files")

    # Generated docs
    generated_src = V1_DATA / "generated"
    if generated_src.exists():
        moved = 0
        for f in generated_src.iterdir():
            dest = user_dir / "generated" / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                moved += 1
        print(f"Generated docs: migrated {moved} files")

    # Cover letters
    cl_src = V1_DATA / "cover_letters"
    if cl_src.exists():
        moved = 0
        for job_dir in cl_src.iterdir():
            if job_dir.is_dir():
                dest_dir = user_dir / "cover_letters" / job_dir.name
                dest_dir.mkdir(parents=True, exist_ok=True)
                for f in job_dir.iterdir():
                    dest = dest_dir / f.name
                    if not dest.exists():
                        shutil.copy2(f, dest)
                        moved += 1
        print(f"Cover letter files: migrated {moved} files")

    # Config
    config_src = V1_DATA / "config.json"
    config_dest = user_dir / "config.json"
    if config_src.exists() and not config_dest.exists():
        shutil.copy2(config_src, config_dest)
        print("Config: migrated config.json")
    else:
        print("Config: skipped (already exists or no source)")

    print(f"\nDone. Data available at: {user_dir}")
    print("Job docx paths in JSON still point to old locations.")
    print("Re-generate resumes/cover letters to get updated paths.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    migrate(sys.argv[1])
