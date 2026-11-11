"""Background board searches with progress polling and a Stop button.

The browser starts one task per board, polls short status requests (so no single HTTP
request has to outlive a reverse-proxy timeout), and can cancel. In-memory only: tasks
vanish on restart, which is fine for a user-initiated interactive search.
"""

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

MAX_ACTIVE_PER_USER = 6
KEEP_SECONDS = 3600
_tasks: dict[str, dict] = {}
_lock = threading.Lock()


class TooManySearches(Exception):
    pass


def _purge_locked() -> None:
    cutoff = time.time() - KEEP_SECONDS
    for tid in [t for t, v in _tasks.items() if v["state"] != "running" and v["finished"] and v["finished"] < cutoff]:
        del _tasks[tid]


def start(req, user_id: str, runner) -> str:
    """Run runner(req, user_id, cancel_event) in a thread; return the task id."""
    with _lock:
        _purge_locked()
        active = sum(1 for v in _tasks.values() if v["user_id"] == user_id and v["state"] == "running")
        if active >= MAX_ACTIVE_PER_USER:
            raise TooManySearches(f"{active} searches are already running. Wait for them or stop them first.")
        tid = uuid.uuid4().hex[:12]
        task = {"id": tid, "user_id": user_id, "state": "running", "started": time.time(), "finished": None,
                "cancel": threading.Event(), "result": None, "error": "", "sites": list(getattr(req, "sites", []))}
        _tasks[tid] = task

    def work():
        try:
            result = runner(req, user_id, task["cancel"])
            task["result"] = result.model_dump()
            task["state"] = "cancelled" if task["cancel"].is_set() else "done"
        except Exception as exc:
            logger.error("Background search %s failed: %s", tid, exc, exc_info=True)
            task["error"] = str(exc)
            task["state"] = "cancelled" if task["cancel"].is_set() else "error"
        finally:
            task["finished"] = time.time()

    threading.Thread(target=work, name=f"search-{tid}", daemon=True).start()
    return tid


def status(task_id: str, user_id: str) -> dict | None:
    with _lock:
        t = _tasks.get(task_id)
    if not t or t["user_id"] != user_id:
        return None
    end = t["finished"] or time.time()
    out = {"id": t["id"], "state": t["state"], "elapsed": round(end - t["started"], 1), "error": t["error"]}
    if t["result"] is not None:
        out["result"] = t["result"]
    return out


def cancel(user_id: str, task_ids: list[str] | None = None) -> int:
    """Signal cancellation of the user's running tasks (all, or just task_ids). Returns how many."""
    n = 0
    with _lock:
        for tid, t in _tasks.items():
            if t["user_id"] == user_id and t["state"] == "running" and (not task_ids or tid in task_ids):
                t["cancel"].set()
                n += 1
    return n
