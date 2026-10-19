"""Archive visibility narrows analytics only, including both historical periods."""

from dataclasses import replace
from datetime import date

import pytest
from fastapi import HTTPException

from app.web.indicators import IndicatorService
from app.web.overview import read_overview
from app.web.repository import Repository
from app.web.sync_policy import SyncPolicy
from tests.test_overview import HIDDEN, A, B, scope, seed_day
from tests.test_overview import db as db


def policy():
    return SyncPolicy(7, None, frozenset([str(B)]))


def repository():
    repo = object.__new__(Repository)
    repo.sync_policy = policy
    return repo


def test_archive_filter_preserves_grants_and_does_not_change_source_scope():
    original = scope(A, B)
    active = repository().analytics_scope(original)
    assert active.ids == [A]
    assert original.ids == [A, B]
    assert repository().analytics_scope(original, True).ids == [A, B]
    assert HIDDEN not in repository().analytics_scope(original, True).ids
    with pytest.raises(HTTPException) as error:
        repository().analytics_scope(replace(original, selected=B))
    assert error.value.status_code == 403
    assert repository().analytics_scope(replace(original, selected=B), True).ids == [B]
    with pytest.raises(HTTPException) as error:
        repository().analytics_scope(scope(B))
    assert error.value.status_code == 403


def test_overview_totals_comparison_rows_and_history_only_restore(db):
    from psycopg.rows import dict_row

    for day, active, archived in [(date(2026, 1, 1), 100, 1000), (date(2026, 1, 2), 200, 2000)]:
        seed_day(
            db,
            day,
            [{"department_id": A, "revenue": active}, {"department_id": B, "revenue": archived}],
        )
    db.row_factory = dict_row
    active = repository().analytics_scope(scope(A, B))
    report = read_overview(db, active, date(2026, 1, 2), date(2026, 1, 2), "day")
    assert report["current"]["totals"]["revenue"] == 200
    assert report["previous"]["totals"]["revenue"] == 100
    assert [row["department_id"] for row in report["restaurants"]] == [A]
    assert report["trend"][0]["current"]["totals"]["revenue"] == 200
    restored = repository().analytics_scope(scope(A, B), True)
    report = read_overview(db, restored, date(2026, 1, 2), date(2026, 1, 2), "day")
    assert report["current"]["totals"]["revenue"] == 2200
    assert report["previous"]["totals"]["revenue"] == 1100


def test_indicators_archives_never_override_sync_policy():
    service = IndicatorService(None)
    try:
        implicit = scope(A, B)
        assert service._department_ids(implicit, policy()) == [str(A)]
        assert service._department_ids(implicit, policy(), True) == [str(A), str(B)]
        stopped = SyncPolicy(8, frozenset([str(A)]), frozenset([str(B)]))
        assert service._department_ids(implicit, stopped, True) == [str(A)]
        with pytest.raises(HTTPException) as error:
            service._department_ids(replace(implicit, selected=B), stopped, True)
        assert error.value.status_code == 423
    finally:
        service.close()


def test_metadata_marks_archives_without_removing_granted_departments():
    original = replace(scope(A, B), user={"role": "manager", "warehouse_scope_mode": "selected"})
    metadata = repository().metadata(original)
    assert [item["id"] for item in metadata["departments"]] == [str(A), str(B)]
    assert [item["archived"] for item in metadata["departments"]] == [False, True]
