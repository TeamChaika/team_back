"""Distinguish saved intraday observations from final daily reports."""

from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Simferopol")


def scope_covered(check, departments):
    """Only explicit selected-scope metadata can prove requested venues were refreshed."""
    if (
        not departments
        or check.get("sync_scope_mode") != "selected"
        or check.get("exact_match") is not True
    ):
        return False
    refreshed = check.get("sync_department_ids")
    if not isinstance(refreshed, list) or not refreshed:
        return False
    try:
        requested = {UUID(str(value)) for value in departments}
        available = {UUID(str(value)) for value in refreshed}
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(requested) and requested <= available


def partial_days(reports, departments=None):
    observations = {}
    for report in reports:
        day, observed = report["business_date"], report["observed_at"]
        frozen = any(
            check.get("partial_scope") and not scope_covered(check, departments)
            for check in report.get("checks", [])
        )
        if frozen or observed < datetime.combine(day + timedelta(days=1), time.min, ZONE):
            observations[day] = min(observed, observations.get(day, observed))
    return [{"date": day, "observed_at": observations[day]} for day in sorted(observations)]
