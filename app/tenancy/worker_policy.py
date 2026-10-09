"""Fresh company capability checks immediately before a worker's first write."""

import os
from pathlib import Path
from uuid import UUID

from app.documents.actors import owner_actor
from app.documents.context import runtime_of
from app.saas_admin.repository import Problem
from app.tenancy.config import load_runtime


def configure(service, verifier=None):
    """Bind a worker (or portal-owned service) to its process company only."""
    runtime = load_runtime()
    if runtime.mode != "tenant":
        return service
    if runtime_of(service.database) != runtime:
        raise ValueError("Worker database does not belong to this runtime")
    if verifier is None:
        from app.tenancy.bootstrap import RestrictedVerifier, VerifierGrant

        secret_file = Path(os.environ["RESTCONTROL_TENANT_VERIFIER_SECRET_FILE"])
        if secret_file.stat().st_mode & 0o077:
            raise ValueError("Verifier secret file must be private")
        verifier = RestrictedVerifier(
            VerifierGrant(
                str(runtime.company_id), "documents-worker", secret_file.read_text().strip()
            ),
            os.environ["RESTCONTROL_TENANT_VERIFIER_SOCKET"],
        )
    if verifier.grant.company_id != str(runtime.company_id) or verifier.grant.role not in {
        "documents-worker",
        "portal",
    }:
        raise ValueError("Verifier does not belong to this document runtime")
    service.feature_authorizer = lambda feature_id: verifier.require_feature(
        feature_id, operation="create"
    )
    service.owner_authorizer = verifier.authorize_platform_actor
    # Retain the private client for the lifetime of the service and its callbacks.
    service._worker_verifier = verifier
    return service


def require_feature(service, feature_id):
    if runtime_of(service.database).mode != "tenant":
        return
    authorizer = getattr(service, "feature_authorizer", None)
    if authorizer is None or not authorizer(feature_id):
        raise Problem(503, "feature_unavailable", "Доступ к функции не подтверждён")


def require_queued_owner(service, operation):
    """Authorize the original submitter afresh; persisted audit is never a grant."""
    runtime = runtime_of(service.database)
    if runtime.mode != "tenant":
        return
    if operation is None:
        raise Problem(503, "submitter_unavailable", "Автор отправки не подтверждён")
    saved = operation.get("actor")
    if not saved or saved.get("kind") != "platform_owner":
        return
    company_id, user_id = UUID(saved["company_id"]), UUID(saved["auth_user_id"])
    if company_id != runtime.company_id:
        raise Problem(403, "tenant_boundary", "Другая компания")
    authorizer = getattr(service, "owner_authorizer", None)
    principal = authorizer(company_id, user_id) if authorizer else None
    if (
        principal is None
        or owner_actor(service.database, principal) is None
        or principal.auth_user_id != user_id
    ):
        raise Problem(403, "owner_revoked", "Доступ владельца не подтверждён")
