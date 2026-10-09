"""Pydantic models shared by the store and the MCP tools.

Every tool returns one of these, so agents always get the same, typed shape back.
"""

from typing import Literal

from pydantic import BaseModel, Field

LeadStatus = Literal["new", "contacted", "qualified", "won", "lost"]

LEAD_STATUSES: tuple[str, ...] = ("new", "contacted", "qualified", "won", "lost")


class Note(BaseModel):
    id: str
    lead_id: str
    body: str
    created_at: str


class Lead(BaseModel):
    id: str
    name: str
    email: str | None = None
    company: str | None = None
    status: LeadStatus
    created_at: str
    updated_at: str


class LeadDetail(Lead):
    notes: list[Note] = Field(default_factory=list)


class LeadPage(BaseModel):
    leads: list[Lead]
    next_cursor: str | None = Field(
        default=None,
        description="Pass this back as `cursor` to get the next page. Null means there are no more results.",
    )
