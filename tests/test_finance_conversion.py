"""Conversion conserves the ledger and keeps original receipts inspectable."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_finance_allocations import DAY, calculate, no_calculation
from test_finance_ledger import expense
from test_finance_ledger import ledger_db as ledger_db

from app.web.finance_allocation_math import AllocationConvert
from app.web.finance_allocations import FinanceAllocations
from app.web.finance_ledger import ExpenseCancel, ExpenseFields


def conversion(source, venues, **changes):
    return AllocationConvert(
        **{
            **{field: source[field] for field in ExpenseFields.model_fields},
            "request_id": uuid4(),
            "revision": source["revision"],
            "method": "equal",
            "parts": [{"department_id": venue} for venue in venues],
            **changes,
        }
    )


def test_paid_manager_receipt_conversion_retry_author_history_and_totals(ledger_db):
    ledger, db, (manager, _, accountant, owner), venues = ledger_db
    original = expense(venues[0], paid_date="2025-11-12", comment="Original receipt")
    source = ledger.create(manager, original)
    groups = FinanceAllocations(ledger.repo)
    payload = conversion(source, venues)
    group = groups.convert(owner, source["id"], payload, calculate(owner))
    assert group["source_expense_id"] == source["id"]
    assert group["author_id"] == str(manager.user["id"])
    assert groups.convert(owner, source["id"], payload, no_calculation)["id"] == group["id"]
    rows = ledger.list(owner, DAY, DAY, limit=100, offset=0)["rows"]
    retired = next(row for row in rows if row["id"] == source["id"])
    assert retired["status"] == "converted" and retired["cancelled"]
    assert not retired["can_edit"] and not retired["can_allocate"]
    assert retired["converted_allocation_id"] == group["id"] and retired["can_view_allocation"]
    assert all(retired[field] == source[field] for field in ExpenseFields.model_fields)
    totals = ledger.totals(owner, DAY, DAY)
    assert totals["expenses"] == totals["allocated_expenses"] == "120.50"
    assert totals["direct_expenses"] == "0.00"
    for venue in venues:
        single = SimpleNamespace(user=owner.user, ids=(venue,))
        assert ledger.totals(single, DAY, DAY)["expenses"] == "60.25"
    own = ledger.list(manager, DAY, DAY, limit=100, offset=0)["rows"]
    assert len(own) == 1 and own[0]["status"] == "converted"
    assert own[0]["converted_allocation_id"] is None and not own[0]["can_view_allocation"]
    history = ledger.history(manager, source["id"])["rows"]
    assert [row["action"] for row in history] == ["convert", "create"]
    assert history[0]["actor"] == "User 3"
    assert history[0]["after"]["status"] == "converted"
    assert (
        "parts" not in history[0]["after"] and "converted_allocation_id" not in history[0]["after"]
    )
    assert groups.history(owner, group["id"])["rows"][0]["actor"] == "User 3"
    assert ledger.create(manager, original)["status"] == "converted"
    for scope in (owner, manager):
        with pytest.raises(HTTPException, match="409"):
            ledger.cancel(scope, source["id"], ExpenseCancel(revision=2))
    with pytest.raises(HTTPException, match="403"):
        groups.convert(manager, source["id"], payload, no_calculation)
    with pytest.raises(HTTPException, match="404"):
        groups.convert(accountant, source["id"], payload, no_calculation)
    with pytest.raises(HTTPException, match="409"):
        groups.convert(
            owner, source["id"], payload.model_copy(update={"request_id": uuid4()}), no_calculation
        )


def test_conversion_closed_source_scope_survives_source_exclusion(ledger_db):
    ledger, db, (manager, _, accountant, owner), venues = ledger_db
    source = ledger.create(manager, expense(venues[0]))
    third = uuid4()
    db.execute(
        "INSERT INTO corporate_nodes(source_id,id,type,name) "
        "VALUES('primary',%s,'DEPARTMENT','Third')",
        (third,),
    )
    db.execute("UPDATE corporate_nodes SET present_in_latest=false WHERE id=%s", (venues[0],))
    owner.ids = (*venues, third)
    groups = FinanceAllocations(ledger.repo)
    payload = conversion(source, [venues[1], third])
    assert ledger.list(owner, DAY, DAY, limit=100, offset=0)["rows"][0]["can_allocate"]
    with pytest.raises(HTTPException, match="403"):
        groups.convert(
            owner, source["id"], payload.model_copy(update={"comment": "changed"}), no_calculation
        )
    group = groups.convert(owner, source["id"], payload, calculate(owner))
    db.execute("DELETE FROM web_department_access WHERE user_id=%s", (accountant.user["id"],))
    for venue in [venues[1], third]:
        db.execute(
            "INSERT INTO web_department_access VALUES(%s,'primary',%s)",
            (accountant.user["id"], venue),
        )
    for operation in [
        lambda: groups.get(accountant, group["id"]),
        lambda: groups.history(accountant, group["id"]),
        lambda: groups.update(accountant, group["id"], payload, no_calculation),
        lambda: groups.convert(accountant, source["id"], payload, no_calculation),
    ]:
        with pytest.raises(HTTPException, match="404"):
            operation()


def test_conversion_correction_and_rollback(ledger_db, monkeypatch):
    ledger, db, (manager, _, _, owner), venues = ledger_db
    source = ledger.create(manager, expense(venues[0]))
    groups = FinanceAllocations(ledger.repo)
    payload = conversion(source, venues, amount="200.00", expense_date="2025-11-29")
    from app.web import finance_allocations

    original = finance_allocations._history
    monkeypatch.setattr(
        finance_allocations,
        "_history",
        lambda *args: (_ for _ in ()).throw(RuntimeError("rollback")),
    )
    with pytest.raises(RuntimeError):
        groups.convert(owner, source["id"], payload, calculate(owner))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "120.50"
    assert db.execute("SELECT count(*) AS n FROM finance_allocations").fetchone()["n"] == 0
    monkeypatch.setattr(finance_allocations, "_history", original)
    group = groups.convert(owner, source["id"], payload, calculate(owner))
    assert group["amount"] == "200.00" and group["expense_date"] == "2025-11-29"
    assert ledger.history(manager, source["id"])["rows"][0]["after"]["amount"] == "120.50"
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "0.00"


def test_concurrent_conversion_and_stale_revision(ledger_db):
    ledger, db, (manager, _, _, owner), venues = ledger_db
    source = ledger.create(manager, expense(venues[0]))
    groups = FinanceAllocations(ledger.repo)
    payload = conversion(source, venues)
    barrier = Barrier(2)

    def convert(request):
        barrier.wait(timeout=10)
        try:
            return groups.convert(owner, source["id"], request, calculate(owner))["id"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(2) as workers:
        responses = list(workers.map(convert, [payload, payload]))
    assert responses[0] == responses[1]
    assert len(groups.history(owner, responses[0])["rows"]) == 1
    another = ledger.create(manager, expense(venues[0]))
    source = another
    payload = conversion(source, venues)
    with ThreadPoolExecutor(2) as workers:
        responses = list(
            workers.map(convert, [payload, payload.model_copy(update={"request_id": uuid4()})])
        )
    assert responses.count(409) == 1
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "241.00"


def test_conversion_manager_target_and_stale_guards(ledger_db):
    ledger, db, (manager, _, accountant, owner), venues = ledger_db
    source = ledger.create(manager, expense(venues[0]))
    groups = FinanceAllocations(ledger.repo)
    payload = conversion(source, venues)
    for scope in (manager, accountant):
        with pytest.raises(HTTPException, match="403"):
            groups.convert(scope, source["id"], payload, no_calculation)
    with pytest.raises(HTTPException, match="409"):
        groups.convert(
            owner, source["id"], payload.model_copy(update={"revision": 2}), no_calculation
        )
    db.execute("UPDATE corporate_nodes SET present_in_latest=false WHERE id=%s", (venues[1],))
    with pytest.raises(HTTPException, match="403"):
        groups.convert(owner, source["id"], payload, no_calculation)
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "120.50"


def test_accountant_converts_to_pending_without_double_count_or_restoration(ledger_db):
    ledger, db, (manager, _, accountant, owner), venues = ledger_db
    source = ledger.create(manager, expense(venues[0], paid_date="2025-11-12"))
    db.execute(
        "INSERT INTO web_department_access VALUES(%s,'primary',%s)",
        (accountant.user["id"], venues[1]),
    )
    groups = FinanceAllocations(ledger.repo)
    payload = conversion(source, venues, method="pending")
    group = groups.convert(accountant, source["id"], payload, no_calculation)
    assert groups.history(accountant, group["id"])["rows"][0]["actor"] == "User 2"
    totals = ledger.totals(owner, DAY, DAY)
    assert totals["expenses"] == totals["pending_expenses"] == "120.50"
    assert totals["direct_expenses"] == totals["allocated_expenses"] == "0.00"
    groups.cancel(accountant, group["id"], ExpenseCancel(revision=1))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "0.00"
    assert groups.convert(accountant, source["id"], payload, no_calculation)["cancelled"]
    assert ledger.list(manager, DAY, DAY, limit=100, offset=0)["rows"][0]["status"] == "converted"
