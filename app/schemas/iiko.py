"""Публичное состояние подключения; секреты в ответы не входят."""

from typing import Literal

from pydantic import BaseModel


class IikoSessionStatus(BaseModel):
    configured: bool
    state: Literal["logged_out", "token_cached", "unknown"]


class IikoAuthResponse(IikoSessionStatus):
    reused: bool


class IikoErrorDetails(BaseModel):
    code: str
    message: str


class IikoErrorResponse(BaseModel):
    error: IikoErrorDetails
