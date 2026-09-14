"""Distinguish saved intraday observations from final daily reports."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Simferopol")


def partial_days(reports):
    observations = {}
    for report in reports:
        day, observed = report["business_date"], report["observed_at"]
        if observed < datetime.combine(day + timedelta(days=1), time.min, ZONE):
            observations[day] = min(observed, observations.get(day, observed))
    return [{"date": day, "observed_at": observations[day]} for day in sorted(observations)]
