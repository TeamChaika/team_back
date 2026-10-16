"""Closed telemetry vocabulary; never persist arbitrary exception or URL data."""

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Screen = Literal[
    "overview",
    "sales",
    "reports",
    "documents",
    "transfers",
    "writeoffs",
    "incoming",
    "outgoing",
    "deposits",
    "management",
    "profile",
    "other",
]
Status = Literal["new", "investigating", "resolved", "dismissed"]


def clean_text(value: str) -> str:
    # User-authored descriptions are deliberate support content. Capability URLs,
    # bearer credentials and common pasted secret assignments are always removed.
    value = re.sub(r"(?i)(?:https?://|www\.)\S+", "[ссылка удалена]", value)
    value = re.sub(r"(?<!\w)/(?:[^\s]*)", "[путь удалён]", value)
    value = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[идентификатор удалён]", value)
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[секрет удалён]", value)
    value = re.sub(
        r"(?i)\b(?:password|passwd|token|secret|api[_-]?key|authorization|cookie|пароль|токен)\s*[:=]\s*[^\s,;]+",
        "[секрет удалён]",
        value,
    )
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[секрет удалён]", value
    )
    value = re.sub(r"[^\S\n]+", " ", value)
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32).strip()


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TicketCreate(Payload):
    client_request_id: UUID
    description: str = Field(min_length=10, max_length=4000)
    screen: Screen = "other"
    event_id: UUID | None = None
    _clean = field_validator("description")(clean_text)


class EventCreate(Payload):
    client_request_id: UUID
    source: Literal["browser"] = "browser"
    code: Literal["browser_error", "unhandled_rejection", "api_error"]
    screen: Screen = "other"
    status_code: int | None = Field(default=None, ge=400, le=599, strict=True)
    request_id: UUID | None = None


class StatusWrite(Payload):
    expected_revision: int = Field(ge=1, strict=True)
    status: Status
    note: str = Field(default="", max_length=2000)
    _clean = field_validator("note")(clean_text)


@dataclass(frozen=True)
class Context:
    company_id: str
    company_name: str
    actor_id: str
    actor_name: str = "Система"


def screen_for_path(path: str) -> str:
    # Only known static prefixes survive; neither path parameters nor query string.
    segments = path.split("/")
    key = segments[2] if len(segments) > 2 and segments[1] == "api" else ""
    aliases = {
        "overview": "overview",
        "sales": "sales",
        "reports": "reports",
        "saved-reports": "reports",
        "documents": "documents",
        "transfers": "transfers",
        "writeoffs": "writeoffs",
        "deposits": "deposits",
        "profile": "profile",
        "administration": "management",
        "operation-settings": "management",
    }
    return aliases.get(key, "other")
