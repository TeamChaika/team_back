from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest
from psycopg.rows import dict_row
from test_reference_sync import db as db

from app.sync_indicator_filters import HISTORY_START, filter_request, publish_filter, read_filters


def test_full_filter_query_includes_history_deleted_orders_and_department():
    body = filter_request("order_type", HISTORY_START, date(2026, 9, 18))
    assert body["groupByRowFields"] == ["Department.Id", "OrderType"]
    assert list(body["filters"]) == ["OpenDate.Typed"]
    assert body["filters"]["OpenDate.Typed"]["from"] == "2023-03-01T00:00:00"
    assert body["filters"]["OpenDate.Typed"]["to"] == "2026-09-19T00:00:00"


def test_dictionary_is_scoped_and_incremental_sync_keeps_older_values(db):
    first, other = UUID(int=10), UUID(int=20)
    rows = [
        {"Department.Id": str(first), "OrderType": "A"},
        {"Department.Id": str(other), "OrderType": "Private"},
        {"Department.Id": str(first), "OrderType": None},
    ]
    publish_filter(db, "order_type", HISTORY_START, date(2026, 9, 17), rows, replace=True)
    publish_filter(
        db,
        "order_type",
        date(2026, 9, 10),
        date(2026, 9, 18),
        [{"Department.Id": str(first), "OrderType": "B"}],
        replace=False,
    )
    db.row_factory = dict_row
    data = read_filters(db, SimpleNamespace(ids=[first]))
    assert data["options"]["order_type"] == ["A", "B"]
    assert len(data["options"]) == 21
    assert data["sync"]["order_type"]["period_start"] == HISTORY_START
    assert data["sync"]["order_type"]["period_end"] == date(2026, 9, 18)
    assert all(not v for v in read_filters(db, SimpleNamespace(ids=[]))["options"].values())


def test_failed_dictionary_publication_preserves_previous_complete_capture(db):
    department = UUID(int=10)
    publish_filter(
        db,
        "order_type",
        HISTORY_START,
        date(2026, 9, 17),
        [{"Department.Id": str(department), "OrderType": "A"}],
        replace=True,
    )
    with pytest.raises(ValueError):
        publish_filter(
            db,
            "order_type",
            HISTORY_START,
            date(2026, 9, 18),
            [{"Department.Id": "invalid", "OrderType": "B"}],
            replace=True,
        )
    assert db.execute("SELECT value FROM chaika.indicator_filter_values").fetchall() == [("A",)]
    assert db.execute("SELECT period_end FROM chaika.indicator_filter_sync").fetchone()[0] == date(
        2026, 9, 17
    )


def test_filters_are_not_exposed_to_browser_database_roles(db):
    assert not db.execute(
        "SELECT has_table_privilege('authenticated','chaika.indicator_filter_values','SELECT')"
    ).fetchone()[0]
    assert not db.execute(
        "SELECT has_table_privilege('anon','chaika.indicator_filter_values','SELECT')"
    ).fetchone()[0]
