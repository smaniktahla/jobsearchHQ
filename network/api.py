"""Session-only, deterministic network directory endpoints."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from auth import get_current_user
from models import User
import storage
from .companies import normalize_company
from .linkedin_csv import MAX_BYTES, parse_linkedin_bytes
from .store import NetworkStore


def enabled_store(user: User = Depends(get_current_user)) -> NetworkStore:
    if not storage.load_config(user.id).network_enabled:
        raise HTTPException(404, "Network unavailable")
    return NetworkStore(user.id)


router = APIRouter(prefix="/api/network", tags=["network"])


async def import_csv(request: Request, store: NetworkStore, dry_run: bool):
    # Bound the stream before buffering; accept raw CSV, not client file paths.
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "Import too large")
        data.extend(chunk)
    parsed = parse_linkedin_bytes(bytes(data))
    if parsed.issues:
        raise HTTPException(422, {"issues": [asdict(i) for i in parsed.issues]})
    return store.import_contacts(parsed.contacts, dry_run=dry_run)


@router.post("/import/preview")
async def preview(request: Request, store: NetworkStore = Depends(enabled_store)):
    return await import_csv(request, store, True)


@router.post("/import/commit")
async def commit(request: Request, store: NetworkStore = Depends(enabled_store)):
    return await import_csv(request, store, False)


@router.get("/contacts")
def contacts(q: str = "", employer: str = "", store: NetworkStore = Depends(enabled_store)):
    return [c for c in store.list_contacts()
            if (not employer or normalize_company(c.company) == normalize_company(employer))
            and (not q or q.casefold() in " ".join((c.first_name, c.last_name, c.email, c.company, c.position, c.notes)).casefold())]


class ContactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    company: str | None = None
    position: str | None = None
    connected_on: str | None = None
    notes: str | None = None
    verified: bool | None = None


@router.patch("/contacts/{contact_id}")
def edit(contact_id: str, data: ContactEdit, store: NetworkStore = Depends(enabled_store)):
    changes = data.model_dump(exclude_unset=True)
    if any(v is None for v in changes.values()):
        raise HTTPException(422, "Contact fields cannot be null")
    try:
        store.update_contact(contact_id, **changes)
    except KeyError:
        raise HTTPException(404, "Contact not found") from None
    return {"ok": True}


@router.delete("/contacts/{contact_id}")
def delete(contact_id: str, store: NetworkStore = Depends(enabled_store)):
    try:
        store.delete_contact(contact_id)
    except KeyError:
        raise HTTPException(404, "Contact not found") from None
    return {"ok": True}


@router.delete("/contacts")
def delete_all(store: NetworkStore = Depends(enabled_store)):
    store.delete_all()
    return {"ok": True}


@router.get("/export")
def export(store: NetworkStore = Depends(enabled_store)):
    return store.export()


@router.get("/review")
def review(store: NetworkStore = Depends(enabled_store)):
    return store.export()["review_queue"]


class ReviewChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str | None = None


@router.post("/review/{review_id}/resolve")
def resolve_review(review_id: str, data: ReviewChoice, store: NetworkStore = Depends(enabled_store)):
    try:
        result = store.resolve_review(review_id, data.target_id)
    except KeyError:
        raise HTTPException(404, "Review item or contact not found") from None
    return {"id": result}


class RelationshipEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str


@router.put("/contacts/{contact_id}/relationship")
def edit_relationship(contact_id: str, data: RelationshipEdit, store: NetworkStore = Depends(enabled_store)):
    try:
        store.update_primary_relationship(contact_id, data.kind)
    except KeyError:
        raise HTTPException(404, "Contact not found") from None
    return {"ok": True}
