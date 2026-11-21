"""In-app feedback: forwards a user's message (optionally with a screenshot) to a ticket service.

Optional: the feature is only active when both ATQ_TICKETS_URL and ATQ_TICKETS_TOKEN are set (empty = off, and the
buttons are not shown). The ticket service is expected to provide:
    POST {url}/tickets          multipart: type, title, body, requester_ref, requester_contact, project, context, screenshot?
    GET  {url}/tickets/mine?requester_ref=...   -> {"tickets": [{id, type, title, status, created_at, updated_at, replies:[{body, sent_at}]}]}
with `Authorization: Bearer <token>`.

Design notes:
  - JSHQ keeps no copy of screenshots or messages: they are streamed to the ticket service.
  - The requester is derived from the session, never from client input, so users can only ever see their own tickets.
  - Nothing from the ticket service's errors, URL or token is ever returned to the browser.
"""
import json
import os
import re
import time
from collections import defaultdict, deque
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from auth import get_current_user
from models import User

router = APIRouter()

TYPES = ("help", "bug", "idea")
MAX_MESSAGE = 5000
MAX_CONTEXT = 8000
MAX_IMAGE = 5 * 1024 * 1024
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PER_HOUR = 10
FRIENDLY_DOWN = "We couldn't send your feedback right now. Please try again in a few minutes."

_recent: dict = defaultdict(deque)          # user id -> timestamps of recent submissions (memory only)


def _cfg():
    return os.environ.get("ATQ_TICKETS_URL", "").strip().rstrip("/"), os.environ.get("ATQ_TICKETS_TOKEN", "").strip()


def enabled() -> bool:
    url, tok = _cfg()
    return bool(url and tok)


def _project() -> str:
    return os.environ.get("JSHQ_FEEDBACK_PROJECT", "jobsearchHQ").strip() or "jobsearchHQ"


async def _atq(method: str, path: str, **kw) -> httpx.Response:
    url, tok = _cfg()
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as c:
        return await c.request(method, url + path, headers={"Authorization": f"Bearer {tok}"}, **kw)


def _requester(user: User) -> str:
    return f"jshq:{user.id}"


def _title(message: str) -> str:
    first = " ".join(message.split())
    return first if len(first) <= 80 else first[:79].rstrip() + "…"


@router.get("/api/feedback/status")
def feedback_status(user: User = Depends(get_current_user)):
    return {"enabled": enabled()}


@router.post("/api/feedback", status_code=201)
async def submit_feedback(
    type: str = Form(...),
    message: str = Form(...),
    context: str = Form("{}"),
    consent: str = Form(""),
    screenshot: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user),
):
    if not enabled():
        raise HTTPException(503, "Feedback isn't switched on for this server.")
    if type not in TYPES:
        raise HTTPException(400, "Please choose what kind of feedback this is.")
    message = message.strip()
    if not message:
        raise HTTPException(400, "Please tell us a little about it first.")
    if len(message) > MAX_MESSAGE:
        raise HTTPException(400, f"That is a bit long. Please keep it under {MAX_MESSAGE} characters.")
    if len(context) > MAX_CONTEXT:
        context = "{}"
    try:
        ctx = json.loads(context)
        ctx = ctx if isinstance(ctx, dict) else {}
    except ValueError:
        ctx = {}
    ctx["submitted_via"] = "jshq-help-drawer"

    image = None
    if screenshot is not None and screenshot.filename:
        if consent.strip().lower() not in ("1", "true", "yes", "on"):
            raise HTTPException(400, "Please confirm that you are happy to include the screenshot.")
        raw = await screenshot.read(MAX_IMAGE + 1)
        if len(raw) > MAX_IMAGE:
            raise HTTPException(413, "That screenshot is too large (5 MB maximum).")
        if not (raw.startswith(PNG_SIG) or raw.startswith(b"\xff\xd8\xff")):
            raise HTTPException(400, "The screenshot must be a PNG or JPEG image.")
        image = raw

    now = time.time()
    q = _recent[user.id]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= PER_HOUR:
        raise HTTPException(429, "You have sent a lot of feedback in the last hour. Please try again a bit later.")

    data = {"type": type, "title": _title(message), "body": message, "requester_ref": _requester(user),
            "requester_contact": f"{user.name} <{user.email}>"[:300], "project": _project(), "context": json.dumps(ctx)}
    files = {"screenshot": ("screenshot.png", image, "image/png")} if image else None
    try:
        r = await _atq("POST", "/tickets", data=data, files=files)
    except (httpx.HTTPError, OSError):
        raise HTTPException(502, FRIENDLY_DOWN)
    if r.status_code == 429:
        raise HTTPException(429, "You have sent a lot of feedback today. Please try again tomorrow.")
    if r.status_code != 201:
        raise HTTPException(502, FRIENDLY_DOWN)
    q.append(now)
    try:
        tid = r.json().get("id", "")
    except ValueError:
        tid = ""
    return {"id": tid, "status": "received"}


@router.get("/api/feedback/mine")
async def my_feedback(user: User = Depends(get_current_user)):
    if not enabled():
        raise HTTPException(503, "Feedback isn't switched on for this server.")
    try:
        r = await _atq("GET", "/tickets/mine", params={"requester_ref": _requester(user)})
    except (httpx.HTTPError, OSError):
        raise HTTPException(502, FRIENDLY_DOWN)
    if r.status_code != 200:
        raise HTTPException(502, FRIENDLY_DOWN)
    try:
        tickets = r.json().get("tickets", [])
    except ValueError:
        raise HTTPException(502, FRIENDLY_DOWN)
    keep = ("id", "type", "title", "status", "created_at", "updated_at", "replies")     # allowlist: nothing else passes through
    return {"tickets": [{k: t.get(k) for k in keep} for t in tickets if isinstance(t, dict)]}
