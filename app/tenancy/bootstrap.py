"""Explicit per-company launch and a narrowly scoped private auth verifier.

The central process owns the repository. Children receive only a capability for
one company and role, never control-plane DSNs or global Auth administrator keys.
"""

import hmac
from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request

from app.saas_admin.repository import Problem
from app.tenancy.actor import ActorContext


@dataclass(frozen=True)
class VerifierGrant:
    company_id: str
    role: str
    secret: str

    def __post_init__(self):
        UUID(self.company_id)
        if (
            self.role not in {"portal", "documents-worker", "payments-worker"}
            or len(self.secret) < 32
        ):
            raise ValueError("Invalid company verifier capability")


def create_verifier_app(repository, grants, *, company_accounts=None, runtime_registry=None):
    """Serve exclusively on an operator-owned UDS, never public routing."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/verify/{company_id}/{operation}")
    def verify(company_id: str, operation: str, body: dict, request: Request):
        secret = request.headers.get("authorization", "").removeprefix("Bearer ")
        grant = next(
            (
                g
                for g in (grants(company_id) if callable(grants) else grants)
                if g.company_id == company_id and hmac.compare_digest(g.secret, secret)
            ),
            None,
        )
        if grant is None:
            raise HTTPException(403, "Invalid runtime capability")
        try:
            if operation == "feature":
                from app.saas_admin.entitlements import FEATURES, evaluate_feature

                feature = body.get("feature")
                if body.get("operation", "create") != "create" or feature not in FEATURES:
                    raise Problem(403, "feature_scope", "Недопустимая проверка возможности")
                if grant.role == "documents-worker" and not feature.startswith(
                    ("documents.", "commercial.")
                ):
                    raise Problem(403, "feature_scope", "Возможность вне области worker")
                if grant.role == "payments-worker" and not feature.startswith("payments."):
                    raise Problem(403, "feature_scope", "Возможность вне области worker")
                company = repository.get(company_id)
                decision = evaluate_feature(
                    company, feature, operation="create", staff_allowed=True, warehouse_allowed=True
                )
                if not decision.allowed:
                    raise Problem(403, "feature_disabled", "Возможность недоступна по подписке")
                if runtime_registry is None:
                    raise Problem(
                        503, "feature_readiness_unavailable", "Готовность модуля не подтверждена"
                    )
                readiness = runtime_registry.feature_readiness(company)
                if readiness.get(feature, {}).get("write") is not True:
                    raise Problem(403, "feature_not_ready", "Этот модуль ещё не настроен")
                return {"allowed": True, "company_version": company["version"]}
            if operation in {"account-create", "password", "recovery"} and grant.role == "portal":
                if company_accounts is None:
                    raise Problem(
                        503, "identity_service_missing", "Управление учётными записями не настроено"
                    )
                if operation == "recovery":
                    return company_accounts.recover_password(
                        company_id, body.get("token", ""), body.get("new_password")
                    )
                if operation == "account-create":
                    if runtime_registry is None:
                        raise Problem(
                            503,
                            "feature_readiness_unavailable",
                            "Готовность модуля не подтверждена",
                        )
                    company = repository.get(company_id)
                    if (
                        runtime_registry.feature_readiness(company)
                        .get("management.users", {})
                        .get("write")
                        is not True
                    ):
                        raise Problem(403, "feature_not_ready", "Этот модуль ещё не настроен")
                    return company_accounts.create(
                        company_id, body.get("token", ""), body.get("csrf", ""), body.get("account")
                    )
                return company_accounts.password(
                    company_id,
                    body.get("token", ""),
                    body.get("csrf", ""),
                    body.get("current_password"),
                    body.get("new_password"),
                )
            if operation == "session" and grant.role == "portal":
                actor, session = repository.tenant_actor_session(body.get("token", ""), company_id)
                return {
                    "actor": actor.as_dict(),
                    "session": {
                        "csrf_token": session["csrf_token"],
                        "must_change_password": session.get("must_change_password", False),
                    },
                }
            if operation == "logout" and grant.role == "portal":
                repository.tenant_actor_session(body.get("token", ""), company_id)
                repository.tenant_logout(body.get("token", ""))
                return {"ok": True}
            if operation == "owner" and grant.role in {
                "portal",
                "documents-worker",
                "payments-worker",
            }:
                actor = repository.authorize_platform_actor(
                    company_id, body.get("auth_user_id", "")
                )
                return {"actor": actor.as_dict() if actor else None}
        except Problem as exc:
            raise HTTPException(exc.status, exc.message) from None
        raise HTTPException(403, "Operation outside runtime role")

    return app


class RestrictedVerifier:
    def __init__(self, grant, socket_path, *, transport=None):
        self.grant = grant
        self.client = httpx.Client(
            transport=transport or httpx.HTTPTransport(uds=str(socket_path)),
            base_url="http://verifier",
            timeout=15,
            trust_env=False,
        )

    def _call(self, company_id, operation, body):
        if str(company_id) != self.grant.company_id:
            raise Problem(403, "tenant_boundary", "Другая компания")
        try:
            response = self.client.post(
                f"/verify/{company_id}/{operation}",
                json=body,
                headers={"authorization": "Bearer " + self.grant.secret},
            )
        except httpx.HTTPError:
            raise Problem(503, "verifier_unavailable", "Сервис авторизации недоступен") from None
        if response.status_code != 200:
            raise Problem(
                response.status_code
                if response.status_code in {400, 401, 403, 409, 422, 429, 503}
                else 503,
                "authorization_failed",
                "Доступ не подтверждён",
            )
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError:
            raise Problem(503, "verifier_unavailable", "Сервис авторизации недоступен") from None

    def _actor(self, value):
        if value is None:
            return None
        actor = ActorContext(
            UUID(value["company_id"]),
            UUID(value["auth_user_id"]),
            value["kind"],
            value["display_name"],
            UUID(value["membership_id"]) if value.get("membership_id") else None,
        )
        if str(actor.company_id) != self.grant.company_id:
            raise Problem(403, "tenant_boundary", "Другая компания")
        return actor

    def require_feature(self, feature_id, *, operation="create"):
        result = self._call(
            self.grant.company_id, "feature", {"feature": feature_id, "operation": operation}
        )
        if result.get("allowed") is not True:
            raise Problem(403, "feature_disabled", "Возможность недоступна по подписке")
        return True

    def create_company_account(self, company_id, token, csrf, account):
        return self._call(
            company_id, "account-create", {"token": token, "csrf": csrf, "account": account}
        )

    def change_company_password(self, company_id, token, csrf, current, new):
        return self._call(
            company_id,
            "password",
            {"token": token, "csrf": csrf, "current_password": current, "new_password": new},
        )

    def recover_company_password(self, company_id, token, new_password):
        return self._call(company_id, "recovery", {"token": token, "new_password": new_password})

    def tenant_actor_session(self, token, company_id):
        result = self._call(company_id, "session", {"token": token})
        return self._actor(result["actor"]), result["session"]

    def tenant_logout(self, token):
        return self._call(self.grant.company_id, "logout", {"token": token})

    def authorize_platform_actor(self, company_id, auth_user_id):
        return self._actor(
            self._call(company_id, "owner", {"auth_user_id": str(auth_user_id)})["actor"]
        )


def build_portal(verifier=None, **kwargs):
    from app.tenancy.config import load_runtime

    runtime = load_runtime()
    if runtime.mode != "tenant":
        raise ValueError("Full SaaS bootstrap requires explicit tenant configuration")
    if verifier is None:
        import os
        from pathlib import Path

        secret_file = Path(os.environ["RESTCONTROL_TENANT_VERIFIER_SECRET_FILE"])
        if secret_file.stat().st_mode & 0o077:
            raise ValueError("Verifier secret file must be private")
        verifier = RestrictedVerifier(
            VerifierGrant(str(runtime.company_id), "portal", secret_file.read_text().strip()),
            os.environ["RESTCONTROL_TENANT_VERIFIER_SOCKET"],
        )
    if verifier.grant.company_id != str(runtime.company_id) or verifier.grant.role != "portal":
        raise ValueError("Verifier does not belong to this portal")
    from app.portal import create_portal
    from app.tenancy.payment_bootstrap import build_payments

    if "payment_service" not in kwargs:
        kwargs["payment_service"] = build_payments(runtime)
    kwargs["payment_service"].feature_authorizer = lambda feature: verifier.require_feature(feature)
    app = create_portal(saas_auth_repository=verifier, **kwargs)
    from fastapi.routing import APIRoute

    def runtime_health():
        import os

        from app.web.assistant import AssistantSettings

        return {
            "company_id": str(runtime.company_id),
            "configuration_version": runtime.configuration_version,
            "process_id": __import__("os").getpid(),
            "integrations_revision": int(
                os.environ.get("RESTCONTROL_TENANT_INTEGRATIONS_REVISION", "1")
            ),
            "assistant_configured": AssistantSettings().configured,
            "payment_origin": runtime.payment_origin,
            "status": "ok",
        }

    app.router.routes.insert(0, APIRoute("/_runtime/health", runtime_health, methods=["GET"]))
    original = app.router.lifespan_context
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            from app.tenancy.worker_policy import configure

            documents = getattr(app.state, "documents", None)
            if hasattr(documents, "database"):
                configure(documents, verifier)
            payments = getattr(app.state, "deposits", None)
            if payments is not None:
                payments.feature_authorizer = lambda feature: verifier.require_feature(feature)
            commercial = getattr(app.state, "commercial_invoices", None)
            if commercial:
                configure(commercial, verifier)
            yield

    app.router.lifespan_context = lifespan
    return app


def build_collector():
    """Factory for an explicit company collector; never used by the public gateway."""
    from fastapi.routing import APIRoute

    from app.tenancy.config import load_runtime

    runtime = load_runtime()
    if runtime.mode != "tenant":
        raise ValueError("Company collector requires explicit tenant runtime")
    from app.main import create_app

    app = create_app()

    def runtime_health():
        return {
            "company_id": str(runtime.company_id),
            "configuration_version": runtime.configuration_version,
            "process_id": __import__("os").getpid(),
            "status": "ok",
        }

    app.router.routes.insert(0, APIRoute("/_runtime/health", runtime_health, methods=["GET"]))
    return app


def main():
    import argparse
    import os
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Launch one company portal on its private socket")
    launch = parser.add_mutually_exclusive_group(required=True)
    launch.add_argument("--config", help="Private JSON environment manifest (0600)")
    launch.add_argument("--collector", action="store_true")
    args = parser.parse_args()
    import json

    if args.collector:
        config = {
            k: v
            for k, v in os.environ.items()
            if k == "RESTCONTROL_RUNTIME_MODE" or k.startswith("RESTCONTROL_TENANT_")
        }
    else:
        config_path = Path(args.config)
        if config_path.is_symlink() or config_path.stat().st_mode & 0o077:
            raise SystemExit("Runtime configuration must have mode 0600")
        config = json.loads(config_path.read_text())
    if config.get("RESTCONTROL_RUNTIME_MODE") != "tenant" or any(
        not (key == "RESTCONTROL_RUNTIME_MODE" or key.startswith("RESTCONTROL_TENANT_"))
        for key in config
    ):
        raise SystemExit("Only explicit tenant configuration is accepted")
    for key in ("RESTCONTROL_TENANT_CENTRAL_GID", "RESTCONTROL_TENANT_PROCESS_UID"):
        if key in os.environ and config.get(key) != os.environ[key]:
            raise SystemExit("Runtime manifest changed its pinned launch identity")
    # Do not inherit legacy, service-role, or operator secrets into the child.
    for key in list(os.environ):
        if key not in {"PATH", "LANG", "LC_ALL", "TZ"}:
            del os.environ[key]
    os.environ.update(config)
    from app.tenancy.config import load_runtime

    runtime = load_runtime()
    from app.saas_admin.runtime_process_identity import bind_socket, validate_child_directory

    validate_child_directory(runtime, os.environ)
    os.umask(0o077)
    import uvicorn

    role = "collector" if args.collector else "portal"
    application = build_collector() if args.collector else build_portal()
    mode = 0o660 if os.environ.get("RESTCONTROL_TENANT_CENTRAL_GID") else 0o600
    with bind_socket(runtime.runtime_path(role + ".sock"), mode) as bound:
        uvicorn.Server(uvicorn.Config(application, access_log=False, proxy_headers=False)).run(
            sockets=[bound]
        )


if __name__ == "__main__":
    main()
