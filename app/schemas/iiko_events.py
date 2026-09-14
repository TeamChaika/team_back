"""Contracts for one-day RMS event capture and database-backed order topology."""

from datetime import date as CalendarDate
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventsDayQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: CalendarDate = Field(ge=CalendarDate(2000, 1, 1), le=CalendarDate(2100, 1, 1))


class EventsSyncQuery(EventsDayQuery):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,49}$")


class EventsCapture(BaseModel):
    snapshot_id: UUID
    source_id: str
    date: CalendarDate
    received_at: datetime
    total: int
    sha256: str
    source_bytes: int
    metadata_sha256: str


class EventsSyncResult(BaseModel):
    run_id: UUID
    snapshot_id: UUID
    source_id: str
    date: CalendarDate
    status: Literal["succeeded"]
    counts: dict[str, int | bool]


class TopologyNode(BaseModel):
    id: str
    kind: Literal["order", "action", "actor", "transfer"]
    label: str
    details: dict[str, Any] = Field(default_factory=dict)


class TopologyEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: str
    evidence_event_ids: list[UUID] = Field(default_factory=list)


class OrderTopology(BaseModel):
    source_id: str
    root_order_id: UUID
    order_ids: list[UUID]
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
    transfers: list[dict[str, Any]]
    coverage: list[dict[str, Any]]
    event_count: int
    algorithm_version: str
    limitations: list[str]
