"""Limit an implicit current forecast to enabled venues without changing grants."""

from app.web.sync_policy import read_policy


def forecast_departments(db, departments, *, explicit=False):
    """Explicit selections stay intact so coverage can reject frozen history."""
    if explicit:
        return list(departments)
    policy = read_policy(db)
    return [
        value
        for value in departments
        if policy.departments is None or str(value) in policy.departments
    ]
