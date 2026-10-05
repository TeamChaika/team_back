"""Money and provenance regressions, with no external price calls."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.documents.costs import ZONE, estimate
from tests.test_native_documents import (  # noqa: F401
    PRODUCT,
    RECEIVER,
    SENDER,
    SOURCE,
    TARGET,
    body,
    database,
    service,  # noqa: F811
)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class BalanceDB:
    def __init__(self, balances=(), age=1, present=True):
        self.now = datetime.now(UTC)
        self.balances = balances
        self.age = age
        self.present = present
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "to_regclass" in sql:
            return Cursor([{"reports": self.present, "items": self.present}])
        if "FROM chaika.store_balance_reports" in sql:
            assert "source_id='primary'" in sql
            assert "accounting_timestamp<=%s AND last_seen_at<=%s" in sql
            return Cursor(
                [
                    {
                        "last_snapshot_id": "snapshot",
                        "accounting_timestamp": (self.now - timedelta(hours=self.age))
                        .astimezone(ZONE)
                        .replace(tzinfo=None),
                    }
                ]
            )
        assert "snapshot_id=%s AND store_id=%s" in sql
        assert "GROUP BY product_id" in sql
        assert params[1] == SOURCE
        return Cursor(self.balances)


@pytest.mark.parametrize(
    ("amount", "value", "expected", "reason"),
    [
        ("3", "10", "1.67", None),
        ("-3", "-10", "1.67", None),
        ("0", "10", None, "nonpositive_balance"),
        ("3", "-10", None, "nonpositive_balance"),
        ("3", "0", None, "nonpositive_balance"),
        ("NaN", "10", None, "invalid_balance"),
        ("3", "Infinity", None, "invalid_balance"),
        ("1e-50", "1", None, "invalid_balance"),
        ("1", "1e50", None, "invalid_balance"),
    ],
)
def test_valuation(amount, value, expected, reason):
    db = BalanceDB([{"product_id": PRODUCT, "amount": Decimal(amount), "value": Decimal(value)}])
    result = estimate(db, SOURCE, [{"product_id": PRODUCT, "amount": 0.5}])
    assert result["total"] == expected
    assert result["items"][0]["reason"] == reason
    assert result["items"][0]["sum"] == expected
    assert result["source_at"].endswith("+03:00")


def test_missing_stale_and_partial_are_not_zero_totals():
    other = uuid4()
    rows = [{"product_id": PRODUCT, "amount": 0.5}, {"product_id": other, "amount": 1}]
    db = BalanceDB([{"product_id": PRODUCT, "amount": 3, "value": 10}])
    result = estimate(db, SOURCE, rows)
    assert result["total"] is None and result["known_total"] == "1.67"
    assert result["unpriced_count"] == 1
    for db, reason in [
        (BalanceDB(age=49), "stale_balance"),
        (BalanceDB(present=False), "missing_balance"),
    ]:
        result = estimate(db, SOURCE, rows)
        assert result["total"] is None and result["known_total"] == "0.00"
        assert result["unpriced_count"] == 2
        assert all(r["reason"] == reason for r in result["items"])
        assert not any("GROUP BY" in sql for sql, _ in db.calls)


def test_historical_cutoff_is_accounting_local_and_observation_aware():
    point = datetime(2026, 1, 1, 12, tzinfo=UTC)
    db = BalanceDB()
    estimate(db, SOURCE, [{"product_id": PRODUCT, "amount": 1}], at=point)
    assert db.calls[1][1] == (datetime(2026, 1, 1, 15), point)


def writeoff_body():
    data = body(reason="Reason", reason_id=1)
    data.pop("counteragent_id")
    return data


def test_create_acl_and_snapshot_without_analytics(service):  # noqa: F811
    for user, payload in [
        (RECEIVER, writeoff_body()),
        (SENDER, {**writeoff_body(), "store_id": str(TARGET)}),
    ]:
        with pytest.raises(HTTPException) as error:
            service.dispatch(user, "POST", "writeoff", payload=payload)
        assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        service.dispatch(SENDER, "POST", "writeoff", payload={**writeoff_body(), "total": "1"})
    assert error.value.status_code == 422
    created = service.dispatch(SENDER, "POST", "writeoff", payload=writeoff_body())
    snapshot = created["cost_estimate"]
    assert snapshot["total"] is None
    detail = service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")
    assert detail["cost_estimate"] == snapshot
    assert service.dispatch(SENDER, "GET", "writeoff")["rows"][0]["cost_estimate"] == snapshot
    service.dispatch(
        RECEIVER,
        "POST",
        f"writeoff/{created['id']}/confirm",
        payload={
            "request_id": str(uuid4()),
            "version": 1,
        },
    )
    assert service.provider.sends == []


def test_real_snapshot_scope_duplicates_immutability_and_runtime_role(service):  # noqa: F811
    from pathlib import Path

    snapshot, foreign = uuid4(), uuid4()
    with service.database.connection() as db:
        db.execute("CREATE SCHEMA chaika")
        db.execute(
            "CREATE TABLE chaika.store_balance_reports (source_id text, "
            "accounting_timestamp timestamp,last_snapshot_id uuid,last_seen_at timestamptz)"
        )
        db.execute(
            "CREATE TABLE chaika.store_balance_items (snapshot_id uuid, "
            "product_id uuid,store_id uuid,amount numeric,sum numeric)"
        )
        db.execute("ALTER TABLE chaika.store_balance_reports ENABLE ROW LEVEL SECURITY")
        db.execute("ALTER TABLE chaika.store_balance_items ENABLE ROW LEVEL SECURITY")
    try:
        # Migration grants and policies must work under the actual private runtime role.
        with service.database.connection() as db:
            migration = Path("migrations/documents/0005_writeoff_cost_estimates.sql").read_text()
            db.execute(migration.replace("BEGIN;", "").replace("COMMIT;", ""))
            for source, sid in [("primary", snapshot), ("foreign", foreign)]:
                db.execute(
                    "INSERT INTO chaika.store_balance_reports VALUES "
                    "(%s,(now() AT TIME ZONE 'Europe/Simferopol')-interval '1 hour',%s,"
                    "now()-interval '30 minutes')",
                    (source, sid),
                )
            for sid, store, amount, value in [
                (snapshot, SOURCE, 2, 20),
                (snapshot, SOURCE, 3, 80),
                (snapshot, TARGET, 5, 5000),
                (foreign, SOURCE, 5, 9999),
            ]:
                db.execute(
                    "INSERT INTO chaika.store_balance_items VALUES (%s,%s,%s,%s,%s)",
                    (sid, PRODUCT, store, amount, value),
                )
            db.execute("SET LOCAL ROLE chaika_iiko_app")
            assert (
                db.execute("SELECT count(*) AS n FROM chaika.store_balance_reports").fetchone()["n"]
                == 1
            )
            assert (
                db.execute("SELECT count(*) AS n FROM chaika.store_balance_items").fetchone()["n"]
                == 3
            )
            result = estimate(db, SOURCE, [{"product_id": PRODUCT, "amount": 0.25}])
            assert result["total"] == "5.00"
        data = writeoff_body()
        data["items"][0]["amount"] = 0.25
        created = service.dispatch(SENDER, "POST", "writeoff", payload=data)
        assert created["cost_estimate"]["total"] == "5.00"
        with service.database.connection() as db:
            db.execute("UPDATE chaika.store_balance_items SET sum=99999")
        detail = service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")
        assert detail["cost_estimate"] == created["cost_estimate"]
        # Legacy documents cannot use a report observed after the document was created.
        with service.database.connection() as db:
            db.execute(
                "UPDATE writeoffs SET cost_estimate=NULL,created_at=now()-interval '2 days' "
                "WHERE id=%s",
                (created["id"],),
            )
        legacy = service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")
        assert legacy["cost_estimate"]["total"] is None
        assert legacy["cost_estimate"]["source_at"] is None
        assert service.provider.sends == []
    finally:
        with service.database.connection() as db:
            db.execute("DROP SCHEMA chaika CASCADE")


def test_recipe_runtime_role_and_observation_cutoff(service):  # noqa: F811
    from pathlib import Path

    ingredient, chart, snapshot, department, unit = [uuid4() for _ in range(5)]
    with service.database.connection() as db:
        db.execute("CREATE SCHEMA chaika")
        for table, columns in {
            "store_balance_reports": (
                "source_id text,accounting_timestamp timestamp,"
                "last_snapshot_id uuid,last_seen_at timestamptz"
            ),
            "store_balance_items": (
                "snapshot_id uuid,product_id uuid,store_id uuid,amount numeric,sum numeric"
            ),
            "products": (
                "source_id text,id uuid,type text,main_unit_id uuid,"
                "present_in_latest boolean,deleted boolean,last_seen_at timestamptz"
            ),
            "stores": (
                "source_id text,id uuid,parent_id uuid,present_in_latest boolean,"
                "last_seen_at timestamptz"
            ),
            "corporate_nodes": (
                "source_id text,id uuid,parent_id uuid,type text,"
                "present_in_latest boolean,last_seen_at timestamptz"
            ),
            "assembly_charts": (
                "source_id text,id uuid,product_id uuid,assembled_amount numeric,"
                "details jsonb,date_from date,date_to date,"
                "last_seen_at timestamptz,last_snapshot_id uuid"
            ),
            "assembly_chart_items": (
                "source_id text,id uuid,chart_id uuid,product_id uuid,"
                "amount_in numeric,details jsonb,present_in_latest boolean"
            ),
            "assembly_chart_scopes": (
                "source_id text,chart_id uuid,business_date date,"
                "present_in_latest boolean,last_snapshot_id uuid"
            ),
        }.items():
            db.execute(f"CREATE TABLE chaika.{table} ({columns})")
            db.execute(f"ALTER TABLE chaika.{table} ENABLE ROW LEVEL SECURITY")
    try:
        with service.database.connection() as db:
            for file in ["0005_writeoff_cost_estimates.sql", "0006_writeoff_recipe_costs.sql"]:
                migration = Path("migrations/documents", file).read_text()
                db.execute(migration.replace("BEGIN;", "").replace("COMMIT;", ""))
            db.execute(
                "INSERT INTO chaika.store_balance_reports VALUES ('primary',"
                "(now() AT TIME ZONE 'Europe/Simferopol')-interval '1 hour',%s,"
                "now()-interval '1 hour')",
                (snapshot,),
            )
            db.execute(
                "INSERT INTO chaika.store_balance_items VALUES (%s,%s,%s,5,100)",
                (snapshot, ingredient, SOURCE),
            )
            for pid, kind in [(PRODUCT, "PREPARED"), (ingredient, "GOODS")]:
                db.execute(
                    "INSERT INTO chaika.products VALUES ('primary',%s,%s,%s,true,false,"
                    "now()-interval '1 hour')",
                    (pid, kind, unit),
                )
            db.execute(
                "INSERT INTO chaika.stores VALUES ('primary',%s,%s,true,now()-interval '1 hour')",
                (SOURCE, department),
            )
            db.execute(
                "INSERT INTO chaika.corporate_nodes VALUES ('primary',%s,NULL,"
                "'DEPARTMENT',true,now()-interval '1 hour')",
                (department,),
            )
            db.execute(
                "INSERT INTO chaika.assembly_charts VALUES ('primary',%s,%s,2,"
                '\'{"product_size_assembly_strategy":"COMMON"}\',current_date-1,NULL,'
                "now()-interval '1 hour',%s)",
                (chart, PRODUCT, snapshot),
            )
            db.execute(
                "INSERT INTO chaika.assembly_chart_scopes VALUES ('primary',%s,"
                "(now() AT TIME ZONE 'Europe/Simferopol')::date,true,%s)",
                (chart, snapshot),
            )
            db.execute(
                "INSERT INTO chaika.assembly_chart_items VALUES ('primary',%s,%s,%s,3,'{}',true)",
                (uuid4(), chart, ingredient),
            )
            db.execute(
                "INSERT INTO chaika.products VALUES ('foreign',%s,'GOODS',%s,true,false,now())",
                (uuid4(), unit),
            )
            db.execute("SET LOCAL ROLE chaika_iiko_app")
            assert db.execute("SELECT count(*) AS n FROM chaika.products").fetchone()["n"] == 2
            assert estimate(db, SOURCE, [{"product_id": PRODUCT, "amount": 1}])["total"] == "30.00"
            assert estimate(db, TARGET, [{"product_id": PRODUCT, "amount": 1}])["total"] is None

        class RefreshBetweenStatements:
            def __init__(self, connection):
                self.connection = connection
                self.refreshed = False

            def execute(self, sql, params=None):
                cursor = self.connection.execute(sql, params)
                if "FROM chaika.assembly_charts c" in sql and not self.refreshed:
                    self.refreshed = True
                    with service.database.connection() as writer:
                        writer.execute(
                            "UPDATE chaika.assembly_charts SET assembled_amount=99,"
                            "last_seen_at=now()+interval '1 hour'"
                        )
                        writer.execute("UPDATE chaika.assembly_chart_items SET amount_in=999")
                return cursor

        with service.database.connection() as db:
            db.execute("SET LOCAL ROLE chaika_iiko_app")
            concurrent = RefreshBetweenStatements(db)
            assert (
                estimate(concurrent, SOURCE, [{"product_id": PRODUCT, "amount": 1}])["total"]
                == "30.00"
            )
            assert concurrent.refreshed
        for assignment in [
            "last_seen_at=now()+interval '1 hour'",
            "last_snapshot_id=gen_random_uuid()",
        ]:
            with service.database.connection() as db:
                db.execute(
                    "UPDATE chaika.assembly_charts SET last_seen_at=now()-interval '1 hour',"
                    "last_snapshot_id=%s,date_to=NULL",
                    (snapshot,),
                )
                db.execute("UPDATE chaika.assembly_charts SET " + assignment)
                db.execute("SET LOCAL ROLE chaika_iiko_app")
                assert estimate(db, SOURCE, [{"product_id": PRODUCT, "amount": 1}])["total"] is None
    finally:
        with service.database.connection() as db:
            db.execute("DROP SCHEMA chaika CASCADE")
