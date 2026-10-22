"""
One-off backfill for the archiving feature.

Existing installs have every job ever scraped sitting in jobs/ with no
dedup index and nothing archived. Run this once (inside the container) to:

  1. Build the dedup index from the current job set (so future scrapes can
     check it directly instead of reading every file).
  2. Run the first archive pass, moving old + terminal/low-scoring jobs to
     jobs_archive/.

Usage (from the jobsearch container):
    docker exec -it jobsearch-v2 python migrate_archive.py
    docker exec -it jobsearch-v2 python migrate_archive.py --max-age-days 60 --min-score 5
    docker exec -it jobsearch-v2 python migrate_archive.py --dry-run
"""

import argparse
from pathlib import Path

import storage
import archiving

USERS_DIR = Path("/app/data/users")


def backfill_dedup_index(user_id: str) -> int:
    jobs = storage.load_all_jobs(user_id, include_archived=True)
    index = [storage.dedup_entry(j, archived=False) for j in jobs]
    storage._save_dedup_index(user_id, index)
    return len(index)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=int, default=archiving.DEFAULT_MAX_AGE_DAYS)
    ap.add_argument("--min-score", type=int, default=archiving.DEFAULT_MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true", help="Report what would be archived, change nothing")
    args = ap.parse_args()

    if not USERS_DIR.exists():
        print("No users directory found — nothing to do.")
        return

    for user_dir in sorted(USERS_DIR.iterdir()):
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        indexed = backfill_dedup_index(user_id)
        print(f"[{user_id}] Dedup index built: {indexed} entries")

        if args.dry_run:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(days=args.max_age_days)
            jobs = storage.load_all_jobs(user_id)
            candidates = [j for j in jobs if archiving.is_archivable(j, cutoff, args.min_score)]
            print(f"[{user_id}] DRY RUN: {len(candidates)} of {len(jobs)} active jobs would be archived")
            for j in candidates[:10]:
                print(f"    - {j.company}: {j.title} (status={j.status}, score={j.score.total if j.score else 'unscored'})")
            if len(candidates) > 10:
                print(f"    ... and {len(candidates) - 10} more")
        else:
            result = archiving.run_archive_pass(user_id, max_age_days=args.max_age_days, min_score=args.min_score)
            print(f"[{user_id}] Archived {result['archived_count']} jobs")


if __name__ == "__main__":
    main()
