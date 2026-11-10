"""Per-user JSON network store. Callers must supply the authenticated user ID.

contacts.json is the atomic source of truth for contacts, relationships, review
items, merge history, and import receipts. Other files are reserved collections.
All reads return detached values. A process-wide lock serializes local writers.
"""
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import storage
from .models import Contact, Relationship
from .resolver import resolve

_LOCK = threading.RLock()
_CACHE = {}
_FIELDS = ("first_name", "last_name", "email", "linkedin_url", "company", "position", "connected_on")


def _empty():
    return {"contacts": {}, "relationships": {}, "review_queue": {}, "merge_log": [], "imports": {}}


class NetworkStore:
    def __init__(self, user_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", user_id):
            raise ValueError("Invalid user ID")
        self.root = storage.get_user_dir(user_id) / "network"
        self.path = self.root / "contacts.json"

    def _read(self):
        if not self.path.exists():
            return _empty()
        stat = self.path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = _CACHE.get(self.path)
        if cached is None or cached[0] != stamp:
            cached = (stamp, json.loads(self.path.read_text(encoding="utf-8")))
            _CACHE[self.path] = cached
        return copy.deepcopy(cached[1])

    @staticmethod
    def _atomic(path: Path, value):
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".network-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(storage.sanitize_unicode(value), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write(self, state):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "imports").mkdir(exist_ok=True)
        for filename, initial in (("companies.json", []), ("matches.json", [])):
            if not (self.root / filename).exists():
                self._atomic(self.root / filename, initial)
        self._atomic(self.path, state)
        _CACHE.pop(self.path, None)

    def export(self):
        with _LOCK:
            state = self._read()
            state["companies"] = json.loads((self.root / "companies.json").read_text()) if (self.root / "companies.json").exists() else []
            state["matches"] = json.loads((self.root / "matches.json").read_text()) if (self.root / "matches.json").exists() else []
            return state

    def list_contacts(self):
        with _LOCK:
            return [Contact.model_validate(c) for c in self._read()["contacts"].values()]

    @staticmethod
    def _identities(state):
        identities = [Contact.model_validate(c) for c in state["contacts"].values()]
        redirects = {e["source_id"]: e["target_id"] for e in state["merge_log"] if e["active"]}
        for entry in state["merge_log"]:
            if not entry["active"]:
                continue
            target = entry["target_id"]
            seen = set()
            while target in redirects and target not in seen:
                seen.add(target)
                target = redirects[target]
            if target in state["contacts"]:
                alias = Contact.model_validate(entry["before_source"])
                alias.id = target
                identities.append(alias)
        return identities

    def import_contacts(self, contacts: list[Contact], *, dry_run: bool = True):
        """Preview never writes. Repeated identical review rows are deduplicated.

        Imported fields can refresh; notes, verification, provenance and all
        manually entered relationships remain owned by the user.
        """
        with _LOCK:
            state = self._read()
            counts = {"created": 0, "updated": 0, "review": 0}
            batch_id = str(uuid4())
            for incoming in contacts:
                incoming = Contact.model_validate(incoming.model_dump())
                resolution = resolve(incoming, self._identities(state))
                if resolution.action == "review":
                    payload = {k: getattr(incoming, k) for k in _FIELDS}
                    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                    if key not in state["review_queue"]:
                        state["review_queue"][key] = {"contact": incoming.model_dump(mode="json"), "candidate_ids": list(resolution.candidate_ids), "reason": resolution.reason}
                    counts["review"] += 1
                elif resolution.action == "match":
                    saved = state["contacts"][resolution.candidate_ids[0]]
                    # A merged source remains an identity alias. Reimporting
                    # it must not replace the chosen canonical person's fields.
                    canonical = resolve(incoming, [Contact.model_validate(saved)])
                    if canonical.action == "match":
                        for field in _FIELDS:
                            if getattr(incoming, field):
                                saved[field] = getattr(incoming, field)
                        saved["updated_at"] = datetime.now(timezone.utc).isoformat()
                    counts["updated"] += 1
                else:
                    # IDs supplied by import callers cannot overwrite stored IDs.
                    incoming.id = str(uuid4())
                    incoming.import_batch_id = batch_id
                    state["contacts"][incoming.id] = incoming.model_dump(mode="json")
                    counts["created"] += 1
            if not dry_run:
                state["imports"][batch_id] = counts.copy()
                self._write(state)
                self._atomic(self.root / "imports" / f"{batch_id}.json", counts)
            return counts

    def update_contact(self, contact_id: str, **changes):
        allowed = set(_FIELDS) | {"notes", "verified"}
        if set(changes) - allowed:
            raise ValueError("Unsupported contact field")
        with _LOCK:
            state = self._read()
            saved = state["contacts"][contact_id]
            saved.update(changes)
            saved["updated_at"] = datetime.now(timezone.utc).isoformat()
            state["contacts"][contact_id] = Contact.model_validate(saved).model_dump(mode="json")
            self._write(state)

    def save_relationship(self, relationship: Relationship):
        with _LOCK:
            state = self._read()
            if relationship.contact_id not in state["contacts"]:
                raise KeyError(relationship.contact_id)
            state["relationships"][relationship.id] = relationship.model_dump(mode="json")
            self._write(state)

    def merge(self, target_id: str, source_id: str):
        """Explicit human-confirmed merge; both original records are retained."""
        if target_id == source_id:
            raise ValueError("Cannot merge a contact into itself")
        with _LOCK:
            state = self._read()
            target, source = state["contacts"][target_id], state["contacts"][source_id]
            entry = {"id": str(uuid4()), "target_id": target_id, "source_id": source_id,
                     "created_at": datetime.now(timezone.utc).isoformat(),
                     "before_target": copy.deepcopy(target), "before_source": copy.deepcopy(source),
                     "relationships": copy.deepcopy(state["relationships"]), "active": True}
            for field in _FIELDS:
                if not target[field]:
                    target[field] = source[field]
            target["notes"] = "\n".join(v for v in (target["notes"], source["notes"]) if v)
            target["verified"] = target["verified"] or source["verified"]
            for rel in state["relationships"].values():
                if rel["contact_id"] == source_id:
                    rel["contact_id"] = target_id
            del state["contacts"][source_id]
            entry["after_target"] = copy.deepcopy(target)
            entry["after_relationships"] = copy.deepcopy(state["relationships"])
            state["merge_log"].append(entry)
            self._write(state)
            return entry["id"]

    def unmerge(self, merge_id: str):
        with _LOCK:
            state = self._read()
            entry = next(e for e in state["merge_log"] if e["id"] == merge_id)
            if not entry["active"]:
                raise ValueError("Merge already reversed")
            target = state["contacts"].get(entry["target_id"])
            if target is None or entry["source_id"] in state["contacts"]:
                raise ValueError("Reverse dependent merges first")
            # Undo only values that still equal the merge result. Later edits
            # remain on the target; the original source is restored separately.
            for field, before in entry["before_target"].items():
                if target.get(field) == entry["after_target"].get(field):
                    target[field] = before
            state["contacts"][entry["source_id"]] = entry["before_source"]
            for rid, before in entry["relationships"].items():
                current = state["relationships"].get(rid)
                if before["contact_id"] == entry["source_id"] and current and current["contact_id"] == entry["target_id"]:
                    current["contact_id"] = entry["source_id"]
            entry["active"] = False
            self._write(state)

    def resolve_review(self, review_id: str, target_id: str | None = None):
        """Human choice: attach to a selected contact, or retain as a new person."""
        with _LOCK:
            state = self._read()
            item = state["review_queue"][review_id]
            contact = Contact.model_validate(item["contact"])
            contact.id = str(uuid4())
            state["contacts"][contact.id] = contact.model_dump(mode="json")
            if target_id is not None and target_id not in state["contacts"]:
                raise KeyError(target_id)
            del state["review_queue"][review_id]
            self._write(state)
            if target_id is not None:
                return self.merge(target_id, contact.id)
            return contact.id

    def delete_contact(self, contact_id: str):
        with _LOCK:
            state = self._read()
            del state["contacts"][contact_id]
            state["relationships"] = {k: r for k, r in state["relationships"].items() if r["contact_id"] != contact_id}
            # Deletion removes historical copies too: unmerge must not resurrect it.
            state["merge_log"] = [e for e in state["merge_log"] if contact_id not in (e["target_id"], e["source_id"])]
            for entry in state["merge_log"]:
                for key in ("relationships", "after_relationships"):
                    entry[key] = {k: r for k, r in entry[key].items() if r["contact_id"] != contact_id}
            state["review_queue"] = {k: r for k, r in state["review_queue"].items() if contact_id not in r["candidate_ids"]}
            self._write(state)

    def delete_all(self):
        import shutil
        with _LOCK:
            if self.root.exists():
                shutil.rmtree(self.root)
            _CACHE.pop(self.path, None)
