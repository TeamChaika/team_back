"""Форматы ответов служебных маршрутов."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ServiceInfoResponse(BaseModel):
    name: str
    version: str
