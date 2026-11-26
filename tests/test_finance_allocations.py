"""Shared accruals, privacy and transaction guarantees on isolated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from test_finance_ledger import expense
from test_finance_ledger import ledger_db as ledger_db

from app.web.finance_allocation_math import AllocationCreate, AllocationEdit, calculate_parts
from app.web.finance_allocations import FinanceAllocations
from app.web.finance_ledger import ExpenseCancel, ExpenseEdit

DAY = date(2025, 11, 30)


def command(venues, **changes):
    return AllocationCreate(
        request_id=uuid4(), expense_date=DAY, category="rent", amount="100.01",
        payment_method="bank", method="equal",
        parts=[{"department_id": venue} for venue in venues], **changes,
    )


def edited(payload, revision=1, **changes):
    return AllocationEdit(**(payload.model_dump(exclude={"request_id"}) | changes),
                          revision=revision)


def calculate(scope):
    return lambda payload, db=None: calculate_parts(scope, None, payload)


def no_calculation(*_):
    pytest.fail("Replay/pending must not recalculate current weights")


def test_posting_idempotency_totals_and_legacy_guards(ledger_db):
    ledger, db, (manager, _, accountant, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues)
    created = allocation.create(owner, payload, calculate(owner))
    assert sum(Decimal(part["amount"]) for part in created["parts"]) == Decimal("100.01")
    assert {part["amount"] for part in created["parts"]} == {"50.00", "50.01"}
    assert allocation.create(owner, payload, no_calculation)["id"] == created["id"]
    replay = payload.model_copy(update={"parts": list(reversed(payload.parts))})
    assert allocation.create(owner, replay, no_calculation)["id"] == created["id"]
    with pytest.raises(HTTPException, match="409"):
        allocation.create(owner, payload.model_copy(update={"amount": Decimal("102")}),
                          no_calculation)
    ledger.create(manager, expense(venues[0]))
    totals = ledger.totals(owner, DAY, DAY)
    assert totals["direct_expenses"] == "120.50"
    assert totals["allocated_expenses"] == "100.01"
    assert totals["expenses"] == "220.51"
    rows = ledger.list(owner, DAY, DAY, limit=100, offset=0)["rows"]
    shares = [row for row in rows if row["allocation_id"]]
    assert all(row["can_edit"] and row["can_view_allocation"] for row in shares)
    filtered_owner = SimpleNamespace(user=owner.user, ids=(venues[0],))
    filtered_share = next(row for row in ledger.list(
        filtered_owner, DAY, DAY, limit=100, offset=0)["rows"] if row["allocation_id"])
    assert filtered_share["can_edit"] and filtered_share["can_view_allocation"]
    for share in shares:
        with pytest.raises(HTTPException, match="403"):
            ledger.cancel(owner, share["id"], ExpenseCancel(revision=1))
        with pytest.raises(HTTPException, match="403"):
            ledger.update(owner, share["id"], ExpenseEdit(
                expense_date=DAY, category="other", amount="1", payment_method="bank", revision=1))
    partial = ledger.list(accountant, DAY, DAY, limit=100, offset=0)["rows"]
    own_share = next(row for row in partial if row["allocation_id"])
    assert not own_share["can_edit"] and not own_share["can_view_allocation"]
    assert "parts" not in ledger.history(accountant, own_share["id"])["rows"][0]["after"]
    assert len(ledger.list(manager, DAY, DAY, limit=100, offset=0)["rows"]) == 1
    with pytest.raises(HTTPException, match="404"):
        allocation.get(accountant, created["id"])
    with pytest.raises(HTTPException, match="404"):
        allocation.history(accountant, created["id"])


def test_manager_cannot_access_own_group_after_role_change(ledger_db):
    ledger, db, (manager, _, accountant, owner), venues = ledger_db
    db.execute("INSERT INTO web_department_access VALUES (%s,'primary',%s)",
               (accountant.user["id"], venues[1]))
    allocation = FinanceAllocations(ledger.repo)
    created = allocation.create(accountant, command(venues), calculate(accountant))
    share = ledger.list(accountant, DAY, DAY, limit=100, offset=0)["rows"][0]
    db.execute("UPDATE web_users SET sections=ARRAY['profit-loss'] WHERE id=%s",
               (accountant.user["id"],))
    assert ledger.list(accountant, DAY, DAY, limit=100, offset=0)["total"] == 0
    with pytest.raises(HTTPException, match="404"):
        ledger.history(accountant, share["id"])
    for operation in (
        lambda: allocation.get(accountant, created["id"]),
        lambda: allocation.pending(accountant, DAY, DAY),
        lambda: allocation.cancel(accountant, created["id"], ExpenseCancel(revision=1)),
        lambda: allocation.create(manager, command(venues), no_calculation),
    ):
        with pytest.raises(HTTPException, match="403"):
            operation()


def test_atomic_update_cancel_and_history(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues)
    created = allocation.create(owner, payload, calculate(owner))
    changed = allocation.update(owner, created["id"], edited(payload, amount="200.02",
                                category="utilities", paid_date="2025-11-02"), calculate(owner))
    assert changed["revision"] == 2
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "200.02"
    assert db.execute("SELECT count(*) AS n FROM finance_expenses").fetchone()["n"] == 2
    assert db.execute("SELECT count(*) AS n FROM finance_expense_history").fetchone()["n"] == 4
    with pytest.raises(HTTPException, match="409"):
        allocation.update(owner, created["id"], edited(payload), no_calculation)
    cancelled = allocation.cancel(owner, created["id"], ExpenseCancel(revision=2))
    assert cancelled["cancelled"] and cancelled["revision"] == 3
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "0.00"
    assert allocation.create(owner, payload, no_calculation)["revision"] == 3
    assert [row["action"] for row in allocation.history(owner, created["id"])["rows"]] == [
        "cancel", "update", "create"]
    rows = ledger.list(owner, DAY, DAY, limit=100, offset=0)["rows"]
    assert all(row["cancelled"] for row in rows)


def test_pending_network_only_then_distributed_and_cancelled(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues).model_copy(update={"method": "pending"})
    created = allocation.create(owner, payload, no_calculation)
    assert all("amount" not in part for part in created["parts"])
    assert ledger.list(owner, DAY, DAY, limit=100, offset=0)["total"] == 0
    assert allocation.pending(owner, DAY, DAY)["total"] == 1
    assert allocation.pending(owner, DAY, DAY, limit=1, offset=1)["rows"] == []
    totals = ledger.totals(owner, DAY, DAY)
    assert totals["expenses"] == totals["pending_expenses"] == "100.01"
    assert totals["allocated_expenses"] == "0.00"
    single = SimpleNamespace(user=owner.user, ids=(venues[0],))
    assert ledger.totals(single, DAY, DAY)["expenses"] == "0.00"
    assert allocation.pending(single, DAY, DAY)["total"] == 1
    allocation.update(owner, created["id"], edited(payload, method="equal"), calculate(owner))
    totals = ledger.totals(owner, DAY, DAY)
    assert totals["expenses"] == totals["allocated_expenses"] == "100.01"
    assert totals["pending_expenses"] == "0.00"
    assert allocation.pending(owner, DAY, DAY)["total"] == 0
    allocation.cancel(owner, created["id"], ExpenseCancel(revision=2))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "0.00"


def test_pending_survives_archived_participant_but_respects_grants(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues).model_copy(update={"method": "pending"})
    created = allocation.create(owner, payload, no_calculation)
    db.execute("UPDATE corporate_nodes SET present_in_latest=false WHERE id=%s", (venues[1],))
    network = SimpleNamespace(user=owner.user, ids=(venues[0],))
    assert ledger.totals(network, DAY, DAY)["pending_expenses"] == "100.01"
    assert allocation.get(owner, created["id"])["can_edit"]
    db.execute("UPDATE web_users SET all_departments=false WHERE id=%s", (owner.user["id"],))
    assert ledger.totals(network, DAY, DAY)["pending_expenses"] == "0.00"
    db.execute("UPDATE web_users SET all_departments=true WHERE id=%s", (owner.user["id"],))
    assert allocation.cancel(owner, created["id"], ExpenseCancel(revision=1))["cancelled"]


def test_access_checked_before_calculation_and_archive_blocks_changes(ledger_db):
    ledger, db, (_, _, accountant, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues)
    with pytest.raises(HTTPException, match="403"):
        allocation.create(accountant, payload, no_calculation)
    created = allocation.create(owner, payload, calculate(owner))
    db.execute("INSERT INTO operation_settings(singleton,settings) VALUES(true,%s)",
               (Jsonb({"analytics": {"archived_department_ids": [str(venues[1])]}}),))
    with pytest.raises(HTTPException, match="403"):
        allocation.update(owner, created["id"], edited(payload), no_calculation)
    assert allocation.create(owner, payload, no_calculation)["id"] == created["id"]
    rows = ledger.list(owner, DAY, DAY, limit=100, offset=0)["rows"]
    assert all(row["can_view_allocation"] and not row["can_edit"] for row in rows)


def test_removed_participants_remain_private_in_group_history(ledger_db):
    ledger, db, (_, _, accountant, owner), venues = ledger_db
    third = uuid4()
    db.execute("INSERT INTO corporate_nodes(source_id,id,type,name) "
               "VALUES('primary',%s,'DEPARTMENT','Third')", (third,))
    owner.ids = (*venues, third)
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues)
    created = allocation.create(owner, payload, calculate(owner))
    changed = edited(payload, parts=[{"department_id": venues[0]}, {"department_id": third}])
    allocation.update(owner, created["id"], changed, calculate(owner))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "100.01"
    accountant.ids = (*venues, third)
    db.execute("INSERT INTO web_department_access VALUES(%s,'primary',%s)",
               (accountant.user["id"], third))
    with pytest.raises(HTTPException, match="404"):
        allocation.get(accountant, created["id"])
    with pytest.raises(HTTPException, match="404"):
        allocation.history(accountant, created["id"])
    allocation.update(owner, created["id"], edited(payload, revision=2), calculate(owner))
    assert db.execute("SELECT count(*) AS n FROM finance_expenses").fetchone()["n"] == 3
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "100.01"


def test_failure_after_first_share_rolls_back_everything(ledger_db, monkeypatch):
    ledger, db, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    from app.web import finance_allocations
    original = finance_allocations._record

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Simulated interrupted transaction")

    monkeypatch.setattr(finance_allocations, "_record", fail_after_insert)
    with pytest.raises(RuntimeError):
        allocation.create(owner, command(venues), calculate(owner))
    for table in ("finance_allocations", "finance_expenses", "finance_allocation_history",
                  "finance_expense_history"):
        assert db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0


def test_update_failure_and_database_balance_constraint(ledger_db, monkeypatch):
    ledger, db, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues)
    created = allocation.create(owner, payload, calculate(owner))
    from app.web import finance_allocations
    monkeypatch.setattr(finance_allocations, "_history", lambda *args: (_ for _ in ()).throw(
        RuntimeError("Simulated history failure")))
    with pytest.raises(RuntimeError):
        allocation.update(owner, created["id"], edited(payload, amount="500"), calculate(owner))
    assert allocation.get(owner, created["id"])["revision"] == 1
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "100.01"
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE finance_expenses SET amount=amount+1 WHERE allocation_id=%s",
                   (UUID(created["id"]),))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "100.01"


def test_concurrent_create_and_revision(ledger_db):
    ledger, _, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues)
    barrier = Barrier(2)

    def create():
        barrier.wait(timeout=10)
        return allocation.create(owner, payload, calculate(owner))

    with ThreadPoolExecutor(2) as workers:
        first, second = [task.result(timeout=20)
                         for task in [workers.submit(create), workers.submit(create)]]
    assert first["id"] == second["id"]
    assert len(allocation.history(owner, first["id"])["rows"]) == 1

    def update(amount):
        barrier.wait(timeout=10)
        try:
            return allocation.update(owner, first["id"], edited(payload, amount=amount),
                                     calculate(owner))["revision"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(2) as workers:
        assert sorted(workers.map(update, ["200", "300"])) == [2, 409]


def test_pending_replaces_archived_circle_before_posting(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    third = uuid4()
    db.execute("INSERT INTO corporate_nodes(source_id,id,type,name) "
               "VALUES('primary',%s,'DEPARTMENT','Third')", (third,))
    owner.ids = (*venues, third)
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues).model_copy(update={"method": "pending"})
    created = allocation.create(owner, payload, no_calculation)
    db.execute("UPDATE corporate_nodes SET present_in_latest=false WHERE id=%s", (venues[1],))
    changed = edited(payload, method="equal",
                     parts=[{"department_id": venues[0]}, {"department_id": third}])
    allocation.update(owner, created["id"], changed, calculate(owner))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "100.01"
    rows = ledger.list(owner, DAY, DAY, limit=100, offset=0)["rows"]
    assert {row["department_id"] for row in rows} == {str(venues[0]), str(third)}


def test_confirmed_zero_share_has_no_monetary_child(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues).model_copy(update={"method": "revenue"})

    def distribution(payload, db):
        return [{"department_id": venues[0], "weight": Decimal(1), "amount": payload.amount},
                {"department_id": venues[1], "weight": Decimal(0), "amount": Decimal(0)}]

    created = allocation.create(owner, payload, distribution)
    assert {part["amount"] for part in created["parts"]} == {"0.00", "100.01"}
    assert db.execute("SELECT count(*) AS n FROM finance_expenses").fetchone()["n"] == 1
    assert ledger.totals(owner, DAY, DAY)["allocated_expenses"] == "100.01"
    allocation.cancel(owner, created["id"], ExpenseCancel(revision=1))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "0.00"


def test_replay_ignores_preview_confirmation_and_rolls_back_invalid_calculator(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    allocation = FinanceAllocations(ledger.repo)
    payload = command(venues)
    created = allocation.create(owner, payload, calculate(owner))
    confirmed = AllocationCreate(**(payload.model_dump() | {"parts": [
        {"department_id": part["department_id"], "amount": part["amount"]}
        for part in created["parts"]]}))
    assert allocation.create(owner, confirmed, no_calculation)["id"] == created["id"]
    bad = command(venues)
    with pytest.raises(ValueError, match="preserve total"):
        allocation.create(owner, bad, lambda payload, db: [
            {"department_id": venue, "weight": "1", "amount": "1.00"} for venue in venues])
    assert db.execute("SELECT count(*) AS n FROM finance_allocations").fetchone()["n"] == 1


def test_allocation_tables_have_rls_and_no_public_access(ledger_db):
    _, db, _, _ = ledger_db
    rows = db.execute(
        "SELECT c.relname,c.relrowsecurity,NOT EXISTS (SELECT FROM "
        "aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a WHERE grantee=0) AS private "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)",
        (["finance_allocations", "finance_allocation_history"],),
    ).fetchall()
    assert len(rows) == 2 and all(row["relrowsecurity"] and row["private"] for row in rows)
