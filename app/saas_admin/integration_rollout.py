"""Durable, narrowly scoped application of company integration credentials.

Private health proves loaded configuration, not successful provider completion.
Telegram getMe is read-only; AI providers are never billed for an acceptance probe.
"""

from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from psycopg.types.json import Jsonb


def invalidate_integrations(db, company, revision, changed):
    """Caller holds the company's provisioning lock in its settings transaction."""
    row = db.execute(
        "SELECT checks FROM runtime_provisioning WHERE company_id=%s "
        "AND configuration_version=%s FOR UPDATE",
        (company["id"], company["version"]),
    ).fetchone()
    if not row:
        return
    checks = deepcopy(row["checks"])
    previous = checks.get("integrations", {})
    changed = set(changed) | (
        set(previous.get("changed", []))
        if previous.get("state") in {"pending", "failed"}
        else set()
    )
    retained = dict(previous.get("services", {}))
    for group, service in (
        ("telegram", "telegram_identity"),
        ("assistant", "assistant_configured"),
    ):
        if group in changed:
            retained.pop(service, None)
    checks["integrations"] = {
        "revision": revision,
        "state": "pending",
        "changed": sorted(changed),
        "services": retained,
    }
    services = checks.get("modules", {}).get("evidence", {}).get("services", {})
    for group, service in (
        ("telegram", "telegram_identity"),
        ("assistant", "assistant_configured"),
    ):
        if group in changed:
            services[service] = False
    db.execute(
        "UPDATE runtime_provisioning SET checks=%s,updated_at=now() "
        "WHERE company_id=%s AND configuration_version=%s",
        (Jsonb(checks), company["id"], company["version"]),
    )


def integration_status(repository, company):
    with repository.connect() as db:
        row = db.execute(
            "SELECT checks FROM runtime_provisioning WHERE company_id=%s "
            "AND configuration_version=%s",
            (company["id"], company["version"]),
        ).fetchone()
    return (
        dict(row["checks"].get("integrations", {"state": "not_applied"}))
        if row
        else {"state": "not_applied"}
    )


def probe_integrations(operator, revision, changed, worker_started):
    """Actual current child evidence, never a user impersonation or synthetic acceptance."""
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(operator.runtime.runtime_path("portal.sock"))),
        base_url="http://runtime",
        timeout=5,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = client.get(
            "/_runtime/health", headers={"host": urlsplit(operator.runtime.api_origin).netloc}
        )
        health = response.json()
    if (
        response.status_code != 200
        or not isinstance(health, dict)
        or any(
            (
                health.get("company_id") != str(operator.runtime.company_id),
                health.get("configuration_version") != operator.runtime.configuration_version,
                health.get("integrations_revision") != revision,
                health.get("status") != "ok",
            )
        )
    ):
        return None
    services = {}
    if "assistant" in changed:
        services["assistant_configured"] = health.get("assistant_configured") is True
    if "telegram" in changed:
        settings = operator.config.get("document_settings", {})
        valid = False
        if settings.get("bot_token"):
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = client.get(
                    "https://api.telegram.org/bot" + settings["bot_token"] + "/getMe"
                )
                body = response.json()
                valid = (
                    response.status_code == 200
                    and isinstance(body, dict)
                    and body.get("ok") is True
                    and isinstance(body.get("result"), dict)
                    and body["result"].get("is_bot") is True
                    and body["result"].get("username") == settings.get("bot_username")
                )
            if valid:
                if worker_started is None:
                    return services
                from .runtime_acceptance import heartbeat

                # A previous process's fresh heartbeat is insufficient after a restart.
                stamp = datetime.fromtimestamp(worker_started, UTC).isoformat()
                if not heartbeat(
                    operator,
                    "SELECT updated_at > '" + stamp + "'::timestamptz "
                    "AND updated_at > now()-interval '90 seconds' AND data='{{}}'::jsonb "
                    "FROM {documents}.native_jobs WHERE name='heartbeat'",
                ):
                    return services
                services["documents_worker"] = True
        services["telegram_identity"] = valid
    return services


def reconcile_integrations(operator, revision, worker_started):
    """Serialize with provisioning/settings and retain pending work across fleet crashes."""
    company = operator.company
    with operator.repo.connect(True) as db:
        locked = db.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
            ("provision:" + str(company["id"]),),
        ).fetchone()["locked"]
        if not locked:
            return
        row = db.execute(
            "SELECT checks FROM runtime_provisioning WHERE company_id=%s "
            "AND configuration_version=%s FOR UPDATE",
            (company["id"], company["version"]),
        ).fetchone()
        if not row:
            return
        checks = deepcopy(row["checks"])
        marker = checks.get("integrations", {})
        if marker.get("revision") != revision or marker.get("state") != "pending":
            return
        # Probe failures keep durable work pending and never restore old service proof.
        try:
            services = probe_integrations(
                operator, revision, marker.get("changed", []), worker_started
            )
        except (httpx.HTTPError, ValueError, OSError):
            return
        if services is None:
            return
        proof = checks.get("modules", {}).get("evidence", {})
        if (
            proof.get("company_id") == str(company["id"])
            and proof.get("configuration_version") == company["version"]
        ):
            proof.setdefault("services", {}).update(services)
        # Explicit deletion is successfully applied; nonempty invalid credentials fail.
        failed = (
            "telegram" in marker.get("changed", [])
            and bool(operator.config.get("document_settings", {}).get("bot_token"))
            and services.get("telegram_identity") is False
        )
        required = {"telegram": "telegram_identity", "assistant": "assistant_configured"}
        complete = all(required[group] in services for group in marker.get("changed", []))
        marker.update(
            state="failed" if failed else "applied" if complete else "pending",
            services={**marker.get("services", {}), **services},
            error_code="telegram_identity_unverified" if failed else None,
            checked_at=datetime.now(UTC).isoformat(),
        )
        db.execute(
            "UPDATE runtime_provisioning SET checks=%s,updated_at=now() "
            "WHERE company_id=%s AND configuration_version=%s",
            (Jsonb(checks), company["id"], company["version"]),
        )
