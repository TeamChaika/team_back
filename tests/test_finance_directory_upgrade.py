"""Upgrade a pre-directory database that already contains approved, converted and split expenses."""

from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_finance_ledger import FINANCE_MIGRATIONS, finance_migration_for_schema
from test_finance_ledger import ledger_db as ledger_db

DIRECTORY = "20261127184000_finance_directory.sql"


@pytest.mark.parametrize("ledger_db", [DIRECTORY], indirect=True)
def test_upgrade_preserves_existing_expenses_and_balanced_shares(ledger_db):
    _, db, scopes, venues = ledger_db
    actor = scopes[0].user["id"]
    first, second = venues
    original, direct, group = uuid4(), uuid4(), uuid4()
    create_expense = (
        "INSERT INTO finance_expenses (id,department_id,author_id,request_id,expense_date,"
        "category,amount,payment_method,allocation_id) VALUES (%s,%s,%s,%s,'2026-04-09',"
        "%s,%s,'bank',%s)"
    )

    # These writes go through the old schema, including its deferred balance checks.
    with db.transaction():
        db.execute(create_expense, (direct, first, actor, uuid4(), "repairs", 25, None))
        db.execute(create_expense, (original, first, actor, uuid4(), "rent", 10, None))
        parts = [{"department_id": str(venue), "amount": "5.00"} for venue in venues]
        db.execute(
            "INSERT INTO finance_allocations (id,author_id,request_id,request_payload,"
            "source_expense_id,expense_date,category,amount,payment_method,method,parts,"
            "access_department_ids) VALUES (%s,%s,%s,'{}',%s,'2026-04-09','rent',10,"
            "'bank','equal',%s,%s)",
            (group, actor, uuid4(), original, Jsonb(parts), list(venues)),
        )
        db.execute(
            "UPDATE finance_expenses SET cancelled=true,converted_allocation_id=%s,"
            "revision=2 WHERE id=%s",
            (group, original),
        )
        for venue in (first, second):
            db.execute(create_expense, (uuid4(), venue, actor, uuid4(), "rent", 5, group))
        db.execute(
            "INSERT INTO finance_expense_history (expense_id,revision,actor_id,action,after_state)"
            " VALUES (%s,1,%s,'create',%s)",
            (original, actor, Jsonb({"category": "rent"})),
        )

    # A failed multi-statement migration leaves a transaction open until rollback.
    schema = db.execute("SELECT current_schema() AS name").fetchone()["name"]
    try:
        db.execute(finance_migration_for_schema(FINANCE_MIGRATIONS / DIRECTORY, schema))
    except Exception:
        db.execute("ROLLBACK")
        raise

    assert db.execute(
        "SELECT category_name,amount,source_expense_id FROM finance_allocations WHERE id=%s",
        (group,),
    ).fetchone() == {
        "category_name": "Аренда",
        "amount": Decimal("10"),
        "source_expense_id": original,
    }
    assert db.execute(
        "SELECT category_name,converted_allocation_id FROM finance_expenses "
        "WHERE id=%s AND cancelled",
        (original,),
    ).fetchone() == {"category_name": "Аренда", "converted_allocation_id": group}
    assert db.execute(
        "SELECT count(*) AS n,sum(amount) AS total FROM finance_expenses WHERE allocation_id=%s "
        "AND category_name='Аренда' AND NOT cancelled",
        (group,),
    ).fetchone() == {"n": 2, "total": Decimal("10")}
    assert (
        db.execute("SELECT category_name FROM finance_expenses WHERE id=%s", (direct,)).fetchone()[
            "category_name"
        ]
        == "Ремонт"
    )
    assert db.execute(
        "SELECT after_state FROM finance_expense_history WHERE expense_id=%s", (original,)
    ).fetchone()["after_state"] == {"category": "rent"}
    legacy_id = uuid4()
    db.execute(create_expense, (legacy_id, first, actor, uuid4(), "rent", 1, None))
    assert (
        db.execute(
            "SELECT category_name FROM finance_expenses WHERE id=%s", (legacy_id,)
        ).fetchone()["category_name"]
        == "Аренда"
    )
