"""User-scoped network records with explicit provenance."""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

Source = Literal["linkedin_import", "manual", "research"]


class NetworkRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: Source = "manual"
    verified: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Contact(NetworkRecord):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    linkedin_url: str = ""
    company: str = ""
    position: str = ""
    connected_on: str = ""
    notes: str = ""
    import_batch_id: str | None = None


class Company(NetworkRecord):
    name: str
    normalized_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    domain: str = ""


class Relationship(NetworkRecord):
    contact_id: str
    company_id: str | None = None
    kind: str = "professional"
    notes: str = ""
    started_on: str | None = None
    ended_on: str | None = None


class ImportBatch(NetworkRecord):
    source: Source = "linkedin_import"
    total_rows: int = Field(default=0, ge=0)
    imported_rows: int = Field(default=0, ge=0)
    rejected_rows: int = Field(default=0, ge=0)
