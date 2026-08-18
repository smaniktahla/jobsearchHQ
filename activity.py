"""
Lightweight in-memory activity tracker.

Surfaces operations kicked off outside the browser (Hermes agent calls via
refresh-and-score, research, etc.) so they're visible somewhere other than
the container logs. Deliberately in-memory only — this is live/transient
status, not a durable audit log (agent_log on each Job already covers that).
Single-process app (uvicorn, no workers), so a module-level dict is safe.
"""
import itertools
import threading
from collections import deque
from datetime import datetime

_lock = threading.Lock()
_counter = itertools.count(1)
_active: dict[int, dict] = {}
_recent: deque = deque(maxlen=200)

# Marks the boundary between "live" events (tracked here, since this process
# started) and "historical" events reconstructed from job storage on request
# (see main.py build_history()). Keeps /api/activity from double-showing an
# event that both this tracker and the storage-reconstruction would produce.
PROCESS_STARTED_AT = datetime.now().isoformat()


def start(op_type: str, job_id: str = "", detail: str = "") -> int:
    """Register an operation as in-flight. Returns an id to pass to finish()."""
    op_id = next(_counter)
    with _lock:
        _active[op_id] = {
            "id": op_id,
            "type": op_type,
            "job_id": job_id,
            "detail": detail,
            "started_at": datetime.now().isoformat(),
        }
    return op_id


def finish(op_id: int, status: str = "done", detail: str = "") -> None:
    """Move an operation from active to the recent-completed ring buffer."""
    with _lock:
        op = _active.pop(op_id, None)
        if op is None:
            return
        op["status"] = status
        op["finished_at"] = datetime.now().isoformat()
        if detail:
            op["detail"] = detail
        _recent.appendleft(op)


def snapshot() -> dict:
    with _lock:
        return {
            "active": list(_active.values()),
            "recent": list(_recent),
        }
