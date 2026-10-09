"""Version-pinned operational evidence, separate from actor/warehouse authorization.

A successful read probe proves a readable service. External commands additionally
require their own worker/configuration/payment evidence. Registry callers must
supply checks from the current provisioning version, never a historical runtime.
"""

from dataclasses import dataclass

from .entitlements import FEATURES, evaluate_feature


@dataclass(frozen=True)
class Dependencies:
    paths: tuple[str, ...]
    read_services: tuple[str, ...] = ()
    write_services: tuple[str, ...] | None = None
    write_check: str | None = None


COMMERCIAL = ("commercial_enabled", "seller_configured")
# Every catalog entry is deliberate: adding a feature without a policy fails closed.
DEPENDENCIES = {
    "analytics.overview": Dependencies(("/api/overview",)),
    "analytics.sales": Dependencies(("/api/sales/daily",)),
    "analytics.indicators": Dependencies(("/api/indicators/filters",)),
    "inventory.catalog": Dependencies(("/api/resources/products",)),
    "inventory.balances": Dependencies(("/api/resources/balances",)),
    "purchases.prices": Dependencies(("/api/purchase-prices",)),
    "purchases.impact": Dependencies(("/api/purchase-prices",)),
    "iiko.history": Dependencies(("/api/resources/invoices",)),
    "iiko.cash_shifts": Dependencies(("/api/resources/cash-shifts",)),
    "iiko.order_events": Dependencies(("/api/resources/events",)),
    "documents.waybills": Dependencies(
        ("/api/documents/waybill", "/api/documents/waybill/options"), write_services=()
    ),
    "documents.writeoffs": Dependencies(
        ("/api/documents/writeoff", "/api/documents/writeoff/options"), write_services=()
    ),
    "documents.approval": Dependencies(
        ("/api/documents/waybill/options", "/api/documents/writeoff/options"), write_services=()
    ),
    "documents.dispatch": Dependencies(
        ("/api/documents/waybill/options", "/api/documents/writeoff/options"),
        write_services=("documents_worker",),
    ),
    "commercial.incoming": Dependencies(
        ("/api/commercial-invoices/purchase", "/api/commercial-invoices/purchase/options"),
        COMMERCIAL,
        ("documents_worker", "commercial_submit_enabled"),
    ),
    "commercial.outgoing": Dependencies(
        ("/api/commercial-invoices/sale", "/api/commercial-invoices/sale/options"),
        COMMERCIAL,
        ("documents_worker", "commercial_submit_enabled"),
    ),
    "commercial.counterparties": Dependencies(
        ("/api/commercial-invoices/purchase/options", "/api/commercial-invoices/sale/options"),
        COMMERCIAL,
        ("documents_worker", "commercial_counterparty_create_enabled"),
    ),
    "commercial.invoice_pdf": Dependencies(
        ("/api/commercial-invoices/sale", "/api/commercial-invoices/sale/options"), COMMERCIAL
    ),
    "employees.management": Dependencies(
        ("/api/employees/options",), write_services=(), write_check="connections"
    ),
    "deposits.bookings": Dependencies(
        ("/api/deposits", "/api/deposits/permissions"), write_services=()
    ),
    "payments.create": Dependencies(
        ("/api/deposits", "/api/deposits/permissions", "/api/payment-settings"),
        write_services=(),
        write_check="payments",
    ),
    "payments.status": Dependencies(("/api/deposits", "/api/deposits/permissions")),
    "management.users": Dependencies(("/api/management/accounts",), write_services=()),
    "management.settings": Dependencies(
        ("/api/management/venues", "/api/payment-settings"), write_services=()
    ),
    "profile.account": Dependencies(("/api/me",), write_services=()),
    "notifications.telegram": Dependencies(
        ("/api/me",), ("telegram_identity",), ("documents_worker",)
    ),
    "assistant.chat": Dependencies(("/api/assistant/status",), ("assistant_configured",), ()),
    "operations.sync": Dependencies(("/api/status",), write_services=("scheduler",)),
}


def _payment_verified(check, company):
    evidence = check.get("evidence", {})
    if check.get("ok") is not True or not isinstance(evidence, dict):
        return False
    terminals = evidence.get("terminals")
    return (
        evidence.get("enabled") is not False
        and isinstance(terminals, list)
        and bool(terminals)
        and all(
            isinstance(item, dict)
            and item.get("configuration_version") == company.get("version")
            and item.get("mode") in {"sandbox", "live"}
            and all(
                isinstance(item.get(key), str) and item[key].strip()
                for key in ("terminal_id", "terminal_version_id", "check_id", "settled_attempt_id")
            )
            for item in terminals
        )
    )


def feature_readiness(company: dict, checks: dict, *, working: bool) -> dict[str, dict]:
    """Return readiness for every known feature; unknown/missing evidence never grants access."""
    modules = checks.get("modules", {})
    evidence = modules.get("evidence", {}) if isinstance(modules, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    pinned = (
        str(evidence.get("company_id")) == str(company.get("id"))
        and isinstance(company.get("version"), int)
        and evidence.get("configuration_version") == company["version"]
    )
    probes = evidence.get("probes", []) if pinned else []
    services = evidence.get("services", {}) if pinned else {}
    services = services if isinstance(services, dict) else {}
    paths = {}
    for probe in probes if isinstance(probes, list) else []:
        if isinstance(probe, dict) and isinstance(probe.get("path"), str):
            # Multiple probes of a route (e.g. prices/impact) must all pass.
            path = probe["path"]
            paths[path] = paths.get(path, True) and probe.get("ok") is True
    result = {}
    for feature in FEATURES:
        deps = DEPENDENCIES.get(feature)
        reasons = []
        if not working:
            reasons.append("working_dashboard_unavailable")
        if not pinned:
            reasons.append("module_evidence_not_current")
        if deps is None:
            reasons.append("feature_policy_missing")
        allowed_read = evaluate_feature(
            company, feature, operation="history", staff_allowed=True, warehouse_allowed=True
        )
        allowed_write = evaluate_feature(
            company, feature, operation="create", staff_allowed=True, warehouse_allowed=True
        )
        if not allowed_read.allowed:
            reasons.append(allowed_read.reason)
        if deps is None or not working or not pinned or not allowed_read.allowed:
            state = "not_checked" if not pinned and working and allowed_read.allowed else "blocked"
            result[feature] = {"state": state, "read": False, "write": False, "reasons": reasons}
            continue
        for path in ("/api/me", *deps.paths):
            if paths.get(path) is not True:
                reasons.append("probe_failed:" + path if path in paths else "probe_missing:" + path)
        for name in deps.read_services:
            if services.get(name) is not True:
                reasons.append(
                    "service_unavailable:" + name
                    if name in services
                    else "service_not_checked:" + name
                )
        read = not reasons
        write = read and deps.write_services is not None and allowed_write.allowed
        if deps.write_services is not None:
            if not allowed_write.allowed and allowed_write.reason not in reasons:
                reasons.append(allowed_write.reason)
            for name in deps.write_services:
                if services.get(name) is not True:
                    write = False
                    reasons.append(
                        "service_unavailable:" + name
                        if name in services
                        else "service_not_checked:" + name
                    )
            if deps.write_check:
                check = checks.get(deps.write_check, {})
                check = check if isinstance(check, dict) else {}
                if deps.write_check == "payments":
                    valid = _payment_verified(check, company)
                else:
                    proof = check.get("evidence", {})
                    count = proof.get("checked_connections") if isinstance(proof, dict) else None
                    valid = check.get("ok") is True and type(count) is int and count > 0
                if not valid:
                    write = False
                    reasons.append(deps.write_check + "_acceptance_required")
        reasons = list(dict.fromkeys(reasons))
        state = "ready" if not reasons else "blocked"
        if reasons and all(
            r.startswith(("probe_missing:", "service_not_checked:")) for r in reasons
        ):
            state = "not_checked"
        result[feature] = {"state": state, "read": read, "write": write, "reasons": reasons}
    return result


def require_feature_ready(readiness: dict, feature_id: str, *, write: bool = False) -> bool:
    """A fail-closed capability predicate; callers retain their existing denial response."""
    feature = readiness.get(feature_id)
    return (
        feature_id in FEATURES
        and isinstance(feature, dict)
        and feature.get("write" if write else "read") is True
    )
