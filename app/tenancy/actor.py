"""A verified global identity acting inside one immutable company context."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class ActorContext:
    company_id: UUID
    auth_user_id: UUID
    kind: Literal["company_member", "platform_owner"]
    display_name: str
    membership_id: UUID | None = None

    def __post_init__(self):
        if self.kind not in ("company_member", "platform_owner"):
            raise ValueError("Invalid actor kind")
        if self.kind == "platform_owner" and self.membership_id is not None:
            raise ValueError("Platform owner must not have a company membership")

    def as_dict(self):
        return {
            "company_id": str(self.company_id),
            "auth_user_id": str(self.auth_user_id),
            "kind": self.kind,
            "display_name": self.display_name,
            "membership_id": str(self.membership_id) if self.membership_id else None,
        }


def actor_from_verified_scope(scope):
    """Never reconstruct trusted actors from client dictionaries."""
    actor = getattr(scope, "actor", None)
    if isinstance(actor, ActorContext) and actor.kind == "platform_owner":
        return actor
    return scope.user["id"]
