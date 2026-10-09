"""Read-only acceptance of a real company portal using its existing owner session."""

from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg

from app.tenancy.sql import render

from .entitlements import evaluate_feature
from .provisioning import PendingCheck


def check_modules(operator):
    company = operator.current(
        str(operator.runtime.company_id), operator.runtime.configuration_version
    )
    session_path = operator.config.get("acceptance_session_file")
    if not session_path:
        raise PendingCheck("acceptance_session_required", {"required": ["acceptance_session_file"]})
    path = Path(session_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise PendingCheck("acceptance_session_invalid", {})
    token = path.read_text().strip()
    acceptance_root = operator.config.get("acceptance_root")
    if acceptance_root:
        from .provisioning_acceptance import renew_owner_acceptance

        expected_path = Path(acceptance_root) / (operator.runtime.key + ".acceptance")
        if path != expected_path:
            raise PendingCheck("acceptance_session_invalid", {})
        renew_owner_acceptance(operator.repo, company, token)
    actor, session = operator.repo.tenant_actor_session(token, str(operator.runtime.company_id))
    if str(actor.company_id) != str(operator.runtime.company_id) or actor.kind != "platform_owner":
        raise PendingCheck("acceptance_owner_required", {})
    if session.get("must_change") or session.get("must_change_password"):
        raise PendingCheck("acceptance_password_change_required", {})
    operator.launch("portal")
    probes = [("/api/me", {}, "json"), ("/api/management/venues", {}, "json")]

    def enabled(feature):
        return evaluate_feature(
            company, feature, operation="create", staff_allowed=True, warehouse_allowed=True
        ).allowed

    period = {"start": operator.config["history_from"], "end": operator.config["history_to"]}
    catalog = {
        "analytics.overview": [("/api/overview", period)],
        "analytics.sales": [("/api/sales/daily", period)],
        "analytics.indicators": [("/api/indicators/filters", {})],
        "inventory.catalog": [("/api/resources/products", {"q": ""})],
        "inventory.balances": [("/api/resources/balances", {})],
        "purchases.prices": [("/api/purchase-prices", period)],
        "purchases.impact": [("/api/purchase-prices", {"include_impact": "true"})],
        "iiko.history": [("/api/resources/invoices", period)],
        "iiko.cash_shifts": [("/api/resources/cash-shifts", period)],
        "iiko.order_events": [("/api/resources/events", period)],
        "employees.management": [("/api/employees/options", {})],
        "operations.sync": [("/api/status", {})],
    }
    for feature, routes in catalog.items():
        if enabled(feature):
            probes.extend((endpoint, params, "json") for endpoint, params in routes)
    for kind, feature in [("waybill", "documents.waybills"), ("writeoff", "documents.writeoffs")]:
        if enabled(feature):
            probes += [
                (f"/api/documents/{kind}" + suffix, {}, content)
                for suffix, content in [("", "json"), ("/options", "json"), ("/export", "csv")]
            ]
    for kind, feature in [("purchase", "commercial.incoming"), ("sale", "commercial.outgoing")]:
        if enabled(feature):
            probes += [
                (f"/api/commercial-invoices/{kind}" + suffix, {}, "json")
                for suffix in ("", "/options")
            ]
    if enabled("deposits.bookings"):
        probes += [
            ("/api/deposits" + suffix, {}, content)
            for suffix, content in [("", "json"), ("/permissions", "json"), ("/export", "xlsx")]
        ]
    if enabled("assistant.chat"):
        probes.append(("/api/assistant/status", {}, "assistant"))
    evidence, missing = [], []
    if enabled("notifications.telegram") and not operator.config.get("document_settings", {}).get(
        "bot_token"
    ):
        missing.append("telegram_bot_required")
    if (
        enabled("notifications.telegram")
        or enabled("documents.dispatch")
        or enabled("commercial.incoming")
        or enabled("commercial.outgoing")
    ):
        with psycopg.connect(operator.config["runtime_dsn"]) as db:
            row = db.execute(
                render(
                    "SELECT updated_at > now()-interval '90 seconds' AND data='{{}}'::jsonb "
                    "FROM {documents}.native_jobs WHERE name='heartbeat'",
                    operator.runtime,
                )
            ).fetchone()
        if not row or row[0] is not True:
            missing.append("documents_worker_not_ready")
    if enabled("operations.sync"):
        with psycopg.connect(operator.config["runtime_dsn"]) as db:
            row = db.execute(
                render(
                    "SELECT available AND heartbeat_at > now()-interval '45 seconds' "
                    "FROM {analytics}.scheduler_runtime WHERE singleton",
                    operator.runtime,
                )
            ).fetchone()
        if not row or row[0] is not True:
            missing.append("scheduler_not_ready")
    if enabled("notifications.telegram") and operator.config.get("document_settings", {}).get(
        "bot_token"
    ):
        settings = operator.config["document_settings"]
        valid_bot = False
        try:
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as bot_client:
                response = bot_client.get(
                    "https://api.telegram.org/bot" + settings["bot_token"] + "/getMe"
                )
                body = response.json()
                valid_bot = (
                    response.status_code == 200
                    and body.get("ok") is True
                    and body.get("result", {}).get("is_bot") is True
                    and body.get("result", {}).get("username") == settings.get("bot_username")
                )
        except (httpx.HTTPError, ValueError):
            pass
        evidence.append({"service": "telegram_identity", "ok": valid_bot})
        if not valid_bot:
            missing.append("telegram_bot_identity_unverified")
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(operator.runtime.runtime_path("portal.sock"))),
        base_url="http://runtime",
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        for endpoint, params, content in probes:
            response = client.get(
                endpoint,
                params=params,
                headers={
                    "host": urlsplit(operator.runtime.api_origin).netloc,
                    "origin": operator.runtime.frontend_origin,
                    "cookie": "saas_tenant_session=" + token,
                },
            )
            valid = response.status_code == 200
            if valid and content in {"json", "assistant"}:
                valid = "application/json" in response.headers.get("content-type", "")
                if valid and endpoint == "/api/me":
                    valid = str(response.json().get("user", {}).get("id")) == str(
                        actor.auth_user_id
                    )
                if valid and content == "assistant":
                    valid = response.json().get("configured") is True
            elif valid:
                expected_type = (
                    "text/csv"
                    if content == "csv"
                    else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                valid = expected_type in response.headers.get("content-type", "") and bool(
                    response.content
                )
            evidence.append({"path": endpoint, "status": response.status_code, "ok": valid})
            if not valid:
                missing.append(endpoint)
    if missing:
        raise PendingCheck(
            "module_configuration_required", {"probes": evidence, "missing": missing}
        )
    return {
        "ok": True,
        "evidence": {
            "company_id": str(operator.runtime.company_id),
            "configuration_version": operator.runtime.configuration_version,
            "probes": evidence,
        },
    }
