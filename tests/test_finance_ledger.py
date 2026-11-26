"""Run the real ledger on isolated, fictional restaurant and staff records."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.web.finance_ledger import ExpenseCancel, ExpenseCreate, ExpenseEdit, FinanceLedger


def expense(venue, **changes):
    return ExpenseCreate.model_validate(
        {
            "department_id": venue,
            "request_id": uuid4(),
            "expense_date": "2025-11-30",
            "category": "rent",
            "amount": "120.50",
            "payment_method": "bank",
            **changes,
        }
    )


def edit(original, revision=1, **changes):
    return ExpenseEdit(
        **(original.model_dump(exclude={"department_id", "request_id"}) | changes),
        revision=revision,
    )


def test_input():
    venue = uuid4()
    for values in [
        {"amount": 0},
        {"amount": "0.001"},
        {"amount": "1000000000000"},
        {"comment": "a" * 501},
        {"paid_date": date.today() + timedelta(days=2)},
    ]:
        with pytest.raises(ValidationError):
            expense(venue, **values)
    assert expense(venue, paid_date="2025-11-01").paid_date == date(2025, 11, 1)


@pytest.fixture()
def ledger_db():
    dsn = os.environ.get("LOW_MARKUP_TEST_DSN")
    if not dsn:
        pytest.skip("loopback PostgreSQL fixture required")
    assert conninfo_to_dict(dsn).get("host") in {"127.0.0.1", "localhost"}
    schema = "finance_test_" + uuid4().hex
    # Same SQL and grants as production, in a fresh private schema.
    migration = next(
        (Path(__file__).resolve().parents[1] / "supabase/migrations").glob(
            "*_finance_expense_ledger.sql"
        )
    ).read_text(encoding="utf-8")
    migration += next(
        (Path(__file__).resolve().parents[1] / "supabase/migrations").glob(
            "*_finance_allocations.sql"
        )
    ).read_text(encoding="utf-8")
    migration = (
        migration.replace("chaika.", schema + ".")
        .replace("PUBLIC, anon, authenticated", "PUBLIC")
        .replace("TO chaika_backend", "TO CURRENT_USER")
    )

    def connection():
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    with connection() as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            db.execute("""
                CREATE TABLE web_users (id uuid PRIMARY KEY, display_name text, role text,
                    active bool default true, sections text[], all_departments bool default false,
                    warehouse_scope_mode text default 'all',
                    password_change_required bool default false);
                CREATE TABLE corporate_nodes (source_id text, id uuid, type text, name text,
                    present_in_latest bool default true, PRIMARY KEY(source_id,id));
                CREATE TABLE web_department_access (user_id uuid, source_id text,
                    department_id uuid);
                CREATE TABLE operation_settings (singleton boolean, revision int, settings jsonb,
                    updated_at timestamptz, updated_by uuid);
            """)
            db.execute(migration)
            venues = (uuid4(), uuid4())
            for i, venue in enumerate(venues):
                db.execute(
                    "INSERT INTO corporate_nodes(source_id,id,type,name) "
                    "VALUES ('primary',%s,'DEPARTMENT',%s)",
                    (venue, f"Test {i}"),
                )
            scopes = []
            for i, sections in enumerate(
                [
                    ["profit-loss"],
                    ["profit-loss"],
                    ["profit-loss", "profit-loss-accounting"],
                    ["profit-loss"],
                ]
            ):
                user = uuid4()
                db.execute(
                    "INSERT INTO web_users(id,display_name,role,sections,all_departments) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (user, f"User {i}", "owner" if i == 3 else "manager", sections, i == 3),
                )
                db.execute(
                    "INSERT INTO web_department_access VALUES (%s,'primary',%s)", (user, venues[0])
                )
                scopes.append(SimpleNamespace(user={"id": user}, ids=venues))

            # Map fixed production table names to this fixture without changing application queries.
            class Mapped:
                def __init__(self, raw):
                    self.raw = raw

                def execute(self, cmd, params=None):
                    return self.raw.execute(cmd.replace("chaika.", schema + "."), params)

            class Pool:
                def open(self):
                    pass

                @contextmanager
                def connection(self):
                    with connection() as raw:

                        class Session(Mapped):
                            transaction = raw.transaction

                        yield Session(raw)

            class Repo:
                _pool = Pool()

                @contextmanager
                def connection(self, *, repeatable=False):
                    with connection() as raw, raw.transaction():
                        raw.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        yield Mapped(raw)

            yield FinanceLedger(Repo()), db, scopes, venues
        finally:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_posting_payment_history_and_isolation(ledger_db):
    ledger, db, (manager, colleague, accountant, owner), (venue, other) = ledger_db
    start, end = date(2025, 11, 1), date(2025, 11, 30)
    original = expense(venue)
    created = ledger.create(manager, original)
    uid = created["id"]
    assert created["can_edit"] and created["amount"] == "120.50"
    assert ledger.create(manager, original)["id"] == uid
    with pytest.raises(HTTPException, match="409"):
        ledger.create(manager, original.model_copy(update={"category": "payroll"}))
    assert ledger.list(colleague, start, end, limit=10, offset=0)["total"] == 0
    with pytest.raises(HTTPException, match="404"):
        ledger.history(colleague, uid)
    with pytest.raises(HTTPException, match="403"):
        ledger.create(manager, expense(other))
    with pytest.raises(HTTPException, match="403"):
        ledger.totals(accountant, start, end)
    paid = ledger.update(manager, uid, edit(original, paid_date="2025-12-05"))
    assert paid["revision"] == 2 and not paid["can_edit"]
    with pytest.raises(HTTPException, match="403"):
        ledger.cancel(manager, uid, ExpenseCancel(revision=2))
    with pytest.raises(HTTPException, match="409"):
        ledger.update(accountant, uid, edit(original, revision=1))
    assert ledger.list(accountant, start, end, limit=1, offset=1) == {"rows": [], "total": 1}
    expected = {
        "expenses": "120.50",
        "direct_expenses": "120.50",
        "allocated_expenses": "0.00",
        "pending_expenses": "0.00",
        "bank": "120.50",
        "cash": "0.00",
        "by_category": [{"category": "rent", "amount": "120.50"}],
    }
    assert ledger.totals(owner, start, end) == expected
    assert ledger.totals(owner, date(2025, 12, 1), date(2025, 12, 31))["expenses"] == "0.00"
    assert ledger.create(manager, original)["revision"] == 2
    assert ledger.cancel(accountant, uid, ExpenseCancel(revision=2))["cancelled"]
    assert ledger.totals(owner, start, end)["expenses"] == "0.00"
    history = ledger.history(accountant, uid)["rows"]
    assert [r["action"] for r in history] == ["cancel", "update", "create"]
    assert history[1]["after"]["paid_date"] == "2025-12-05"
    assert history[1]["before"]["paid_date"] is None
    db.execute(
        "UPDATE web_users SET warehouse_scope_mode='selected' WHERE id=%s", (manager.user["id"],)
    )
    with pytest.raises(HTTPException, match="403"):
        ledger.history(manager, uid)


def test_report_rejects_venue_access_narrowed_since_scope_selection(ledger_db):
    ledger, db, (_, _, _, owner), _ = ledger_db
    db.execute("UPDATE web_users SET all_departments=false WHERE id=%s", (owner.user["id"],))
    with pytest.raises(HTTPException, match="403"):
        ledger.totals(owner, date(2025, 11, 1), date(2025, 11, 30))


def test_archived_or_missing_venues_are_read_only(ledger_db):
    ledger, db, (manager, _, _, owner), (venue, _) = ledger_db
    item = expense(venue)
    created = ledger.create(manager, item)
    db.execute("UPDATE corporate_nodes SET present_in_latest=false WHERE id=%s", (venue,))
    rows = ledger.list(manager, item.expense_date, item.expense_date, limit=10, offset=0)["rows"]
    assert rows[0]["id"] == created["id"] and not rows[0]["can_edit"]
    assert ledger.create(manager, item)["id"] == created["id"]
    with pytest.raises(HTTPException, match="403"):
        ledger.create(owner, expense(venue))
    with pytest.raises(HTTPException, match="403"):
        ledger.update(owner, created["id"], edit(item))
    db.execute("UPDATE corporate_nodes SET present_in_latest=true")
    db.execute(
        "INSERT INTO operation_settings(singleton,settings) VALUES(true,%s)",
        (Jsonb({"analytics": {"archived_department_ids": [str(venue)]}}),),
    )
    with pytest.raises(HTTPException, match="403"):
        ledger.cancel(owner, created["id"], ExpenseCancel(revision=1))


def test_racing_creates_and_revisions(ledger_db):
    ledger, _, (manager, _, _, _), (venue, _) = ledger_db
    original = expense(venue)
    start = Barrier(2)

    def create():
        start.wait(timeout=10)
        return ledger.create(manager, original)

    with ThreadPoolExecutor(2) as workers:
        a, b = [job.result(timeout=20) for job in [workers.submit(create), workers.submit(create)]]
    assert a["id"] == b["id"]
    assert len(ledger.history(manager, a["id"])["rows"]) == 1

    def update(amount):
        start.wait(timeout=10)
        try:
            return ledger.update(manager, a["id"], edit(original, amount=amount))["revision"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(2) as workers:
        revisions = list(workers.map(update, ["110.00", "150.00"]))
    assert sorted(revisions) == [2, 409]
    assert len(ledger.history(manager, a["id"])["rows"]) == 2
