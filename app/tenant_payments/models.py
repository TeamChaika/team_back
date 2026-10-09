"""Trusted actor and request contracts for the existing deposit interface."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.tenancy.actor import ActorContext
from app.web.deposits import DepositFilters, NewDeposit

__all__ = ["DepositFilters", "NewDeposit", "PaymentPrincipal", "VenueInput", "TerminalInput"]


@dataclass(frozen=True)
class PaymentPrincipal:
    """Only construct from a freshly verified server-side company scope."""

    actor: ActorContext
    active: bool
    sections: tuple[str, ...]
    can_manage: bool = False
    warehouse_restricted: bool = False
    profile_revision: int | None = None

    def require(self, company_id: UUID, *, manage: bool = False) -> None:
        if not isinstance(self.actor, ActorContext) or self.actor.company_id != company_id:
            raise HTTPException(403, "Чужая компания.")
        if not self.active:
            raise HTTPException(403, "Доступ отключён.")
        if self.actor.kind == "platform_owner":
            return
        if "deposits" not in self.sections or self.warehouse_restricted:
            raise HTTPException(403, "Нет доступа к депозитам.")
        if manage and not self.can_manage:
            raise HTTPException(403, "Недостаточно прав для управления оплатой.")


class VenueInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    active: bool = True
    revision: int | None = Field(default=None, ge=1)


class TerminalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    merchant_id: str | None = Field(default=None, max_length=200)
    mode: Literal["sandbox", "live"]
    active: bool = True
    make_default: bool = False
    revision: int | None = Field(default=None, ge=1)
