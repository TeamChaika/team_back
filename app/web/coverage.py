"""Distinguish saved intraday observations from final daily reports."""

from collections import defaultdict
from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Simferopol")


def _includes_departments(refreshed, departments):
    if not isinstance(refreshed, list) or not refreshed:
        return False
    try:
        requested = {UUID(str(value)) for value in departments}
        available = {UUID(str(value)) for value in refreshed}
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(requested) and requested <= available


def scope_covered(check, departments):
    """Only explicit selected-scope metadata can prove requested venues were refreshed."""
    return (
        check.get("sync_scope_mode") == "selected"
        and check.get("exact_match") is True
        and _includes_departments(check.get("sync_department_ids"), departments)
    )


def has_incomplete_scope(report, departments):
    return any(
        check.get("partial_scope") and not scope_covered(check, departments)
        for check in report.get("checks", [])
    )


def financial_scope_gaps(reports, rows, departments):
    """An absent daily row is zero only with evidence its venue was actually covered."""
    selected = set(departments)
    seen = defaultdict(set)
    gaps = set()
    for row in rows:
        day = row["business_date"]
        seen[day].add(row["department_id"])
        if row.get("frozen"):
            gaps.add(day)
    for report in reports:
        day = report["business_date"]
        absent = selected - seen[day]
        if has_incomplete_scope(report, departments):
            gaps.add(day)
        elif (restriction := report.get("department_filter")) is not None:
            # The stored iiko request also proves which venues a live capture requested.
            if (
                not isinstance(restriction, dict)
                or restriction.get("filterType") != "IncludeValues"
                or not _includes_departments(restriction.get("values"), selected)
            ):
                gaps.add(day)
        elif absent and not any(
            (
                check.get("sync_scope_mode") == "all"
                and check.get("exact_match") is True
                and check.get("partial_scope") is False
            )
            or scope_covered(check, absent)
            for check in report["checks"]
        ):
            gaps.add(day)
    return sorted(gaps)


def partial_days(reports, departments=None):
    observations = {}
    for report in reports:
        day, observed = report["business_date"], report["observed_at"]
        frozen = has_incomplete_scope(report, departments)
        if frozen or observed < datetime.combine(day + timedelta(days=1), time.min, ZONE):
            observations[day] = min(observed, observations.get(day, observed))
    return [{"date": day, "observed_at": observations[day]} for day in sorted(observations)]
