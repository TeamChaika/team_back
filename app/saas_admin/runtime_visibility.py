"""Reduce private portal navigation to accepted company capabilities, never expand ACLs."""

from typing import Any

SECTION_FEATURES = {
    "overview": ("analytics.overview",),
    "sales": ("analytics.sales",),
    "indicators": ("analytics.indicators",),
    "products": ("inventory.catalog",),
    "charts": ("inventory.catalog",),
    "balances": ("inventory.balances",),
    "purchase-prices": ("purchases.prices", "purchases.impact"),
    "cash-shifts": ("iiko.cash_shifts",),
    "events": ("iiko.order_events",),
    "employees": ("employees.management",),
    "invoices": ("iiko.history", "commercial.incoming"),
    "outgoing": ("iiko.history", "commercial.outgoing"),
    "transfers": ("documents.waybills",),
    "writeoffs": ("documents.writeoffs",),
    "deposits": ("deposits.bookings",),
    "status": ("operations.sync",),
}


def accepted_metadata(
    metadata: dict[str, Any], readiness: dict[str, dict[str, Any]], *, working: bool
) -> dict[str, Any]:
    result = {**metadata, "working_dashboard_available": working, "feature_readiness": readiness}
    if not working:
        # Existing accepted history and the owner setup allowlist keep their
        # original ACLs. Neither case permits new business mutations.
        return result

    def readable(feature: str) -> bool:
        return readiness.get(feature, {}).get("read") is True

    sections = [
        section
        for section in metadata.get("sections", [])
        if any(readable(feature) for feature in SECTION_FEATURES.get(section, ()))
    ]
    result["sections"] = sections
    if isinstance(metadata.get("user"), dict) and "sections" in metadata["user"]:
        result["user"] = {
            **metadata["user"],
            "sections": [
                section for section in metadata["user"]["sections"] if section in sections
            ],
        }
    result["can_manage"] = bool(metadata.get("can_manage")) and any(
        readable(feature) for feature in ("management.users", "management.settings")
    )
    result["documents_enabled"] = bool(metadata.get("documents_enabled")) and any(
        readable(feature) for feature in ("documents.waybills", "documents.writeoffs")
    )
    result["live_sales_enabled"] = bool(metadata.get("live_sales_enabled")) and any(
        readable(feature) for feature in ("analytics.overview", "analytics.sales")
    )
    return result
