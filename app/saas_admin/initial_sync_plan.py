"""Versioned coverage contract for the existing tenant history loaders."""

from datetime import date, timedelta

INITIAL_SYNC_PLAN_VERSION = 2
DOCUMENT_RESOURCES = ("writeoffs", "invoices", "outgoing", "transfers")
REQUIRED_JOBS = (
    "sync_references",
    "sync_inventory",
    "sync_employees",
    "sync_dictionaries",
    "sync_store_balances",
    "sync_sales_history",
    "sync_documents",
    "sync_event_history",
    "sync_cash_shifts",
)


def initial_sync_plan(start: date, end: date) -> dict:
    if start > end:
        raise ValueError("Invalid initial history interval")
    windows = []
    current = start
    while current <= end:
        last = min(current + timedelta(days=6), end)
        windows.append({"date_from": str(current), "date_to": str(last)})
        current = last + timedelta(days=1)
    return {
        "plan_version": INITIAL_SYNC_PLAN_VERSION,
        "history_from": str(start),
        "history_to": str(end),
        "document_resources": list(DOCUMENT_RESOURCES),
        "cash_shift_windows": windows,
    }


def initial_sync_compatible(check: object) -> bool:
    if not isinstance(check, dict) or check.get("ok") is not True:
        return False
    proof = check.get("evidence")
    if not isinstance(proof, dict):
        return False
    try:
        plan = initial_sync_plan(
            date.fromisoformat(proof["history_from"]), date.fromisoformat(proof["history_to"])
        )
    except (KeyError, TypeError, ValueError):
        return False
    return all(proof.get(key) == value for key, value in plan.items()) and (
        isinstance(proof.get("completed_jobs"), list)
        and all(isinstance(job, str) for job in proof["completed_jobs"])
        and set(REQUIRED_JOBS) <= set(proof["completed_jobs"])
        and proof["completed_jobs"].count("sync_cash_shifts") == len(plan["cash_shift_windows"])
    )
