"""Venue-specific authorization to record who approved a fictional expense."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_finance_allocations import DAY, calculate, command, edited, no_calculation
from test_finance_ledger import edit, expense
from test_finance_ledger import ledger_db as ledger_db

from app.web.finance_allocation_math import AllocationCreate
from app.web.finance_allocations import FinanceAllocations
from app.web.finance_ledger import ExpenseCancel


def configure(db, venues):
    first, second = venues
    shared, north, south = uuid4(), uuid4(), uuid4()
    for approver_id, label, assigned in (
        # A global person assigned explicitly to one venue must appear there once.
        (shared, "Network partner", (first,)),
        (north, "Northern manager", (first,)),
        (south, "Southern manager", (second,)),
    ):
        db.execute(
            "INSERT INTO finance_approvers(id,name,available_to_all) VALUES (%s,%s,%s)",
            (approver_id, label, approver_id == shared),
        )
        for department_id in assigned:
            db.execute(
                "INSERT INTO finance_department_approvers(department_id,approver_id) "
                "VALUES (%s,%s)",
                (department_id, approver_id),
            )
    return shared, north, south


def test_directory_and_expense_enforce_the_authors_venue(ledger_db):
    ledger, db, (manager, colleague, _, owner), venues = ledger_db
    shared, north, south = configure(db, venues)
    rows = ledger.approvers(manager)["rows"]
    assert {(str(r["department_id"]), r["name"]) for r in rows} == {
        (str(venues[0]), "Network partner"), (str(venues[0]), "Northern manager"),
    }
    assert len(rows) == 2
    third = uuid4()
    db.execute(
        "INSERT INTO corporate_nodes(source_id,id,type,name) VALUES('primary',%s,'DEPARTMENT',%s)",
        (third, "Third test venue"),
    )
    owner.ids = (*venues, third)
    owner_options = ledger.approvers(owner)["rows"]
    assert len(owner_options) == 5
    assert [r["name"] for r in owner_options if str(r["department_id"]) == str(third)] == [
        "Network partner"
    ]
    with pytest.raises(HTTPException, match="422"):
        ledger.create(owner, expense(third))
    with pytest.raises(HTTPException, match="422"):
        ledger.create(owner, expense(third, approver_id=south))
    global_expense = ledger.create(owner, expense(third, approver_id=shared))
    assert global_expense["approver_name"] == "Network partner"
    for approval in (None, south, uuid4()):
        with pytest.raises(HTTPException, match="422"):
            ledger.create(manager, expense(venues[0], approver_id=approval))
    original = expense(venues[0], approver_id=north)
    row = ledger.create(manager, original)
    assert row["approver_id"] == str(north) and row["approver_name"] == "Northern manager"
    assert ledger.create(manager, original)["id"] == row["id"]
    with pytest.raises(HTTPException, match="409"):
        ledger.create(manager, original.model_copy(update={"approver_id": shared}))
    with pytest.raises(HTTPException, match="404"):
        ledger.history(colleague, row["id"])
    updated = ledger.update(manager, row["id"], edit(original, approver_id=shared))
    assert updated["approver_name"] == "Network partner"
    listed = ledger.list(manager, DAY, DAY, limit=10, offset=0)["rows"]
    assert listed[0]["approver_id"] == str(shared)
    change = ledger.history(owner, row["id"])["rows"][0]
    assert change["before"]["approver_name"] == "Northern manager"
    assert change["after"]["approver_name"] == "Network partner"
    assert change["actor"] == "User 0"
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "241.00"


def test_preexisting_expense_remains_visible_without_an_invented_approver(ledger_db):
    ledger, db, (manager, _, _, _), venues = ledger_db
    before_directory = expense(venues[0])
    historic = ledger.create(manager, before_directory)
    # This is the actual JSON shape of a creation snapshot from before the migration.
    db.execute(
        "UPDATE finance_expense_history SET after_state=after_state-'approver_id'-'approver_name' "
        "WHERE expense_id=%s", (historic["id"],),
    )
    configure(db, venues)
    row = ledger.list(manager, DAY, DAY, limit=10, offset=0)["rows"][0]
    assert row["id"] == historic["id"] and row.get("approver_name") is None
    assert ledger.create(manager, before_directory)["id"] == historic["id"]
    assert ledger.cancel(manager, historic["id"], ExpenseCancel(revision=1))["cancelled"]


def test_allocations_validate_each_venue_and_copy_only_its_approval_to_ledger(ledger_db):
    ledger, db, (_, _, accountant, owner), venues = ledger_db
    groups = FinanceAllocations(ledger.repo)
    shared, north, south = configure(db, venues)
    parts = [
        {"department_id": venues[0], "approver_id": north},
        {"department_id": venues[1], "approver_id": south},
    ]
    fields = command(venues).model_dump()
    wrong = [parts[0], {**parts[1], "approver_id": north}]
    for method in ("equal", "pending"):
        request = AllocationCreate(**(fields | {"method": method, "parts": wrong}))
        with pytest.raises(HTTPException, match="422"):
            groups.create(owner, request, calculate(owner))
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "0.00"
    request = AllocationCreate(**(fields | {"parts": parts, "method": "pending"}))
    group = groups.create(owner, request, no_calculation)
    assert {p["approver_name"] for p in group["parts"]} == {"Northern manager", "Southern manager"}
    assert groups.create(owner, request, no_calculation)["id"] == group["id"]
    different = [parts[0], {**parts[1], "approver_id": shared}]
    with pytest.raises(HTTPException, match="409"):
        groups.create(owner, AllocationCreate(**(request.model_dump() | {"parts": different})),
                      no_calculation)
    posted = groups.update(owner, group["id"], edited(request, method="equal"), calculate(owner))
    assert posted["method"] == "equal"
    rows = ledger.list(accountant, DAY, DAY, limit=10, offset=0)["rows"]
    assert len(rows) == 1 and rows[0]["approver_name"] == "Northern manager"
    assert not rows[0]["can_view_allocation"]
    history = ledger.history(accountant, rows[0]["id"])["rows"]
    assert history[0]["after"]["approver_id"] == str(north)
    assert "Southern manager" not in str(history)
    assert ledger.totals(owner, DAY, DAY)["expenses"] == "100.01"


def test_unmodified_approvers_survive_directory_changes(ledger_db):
    ledger, db, (manager, _, _, _), venues = ledger_db
    _, north, _ = configure(db, venues)
    original = expense(venues[0], approver_id=north)
    row = ledger.create(manager, original)
    db.execute("UPDATE finance_approvers SET name='New name' WHERE id=%s", (north,))
    db.execute("DELETE FROM finance_department_approvers WHERE approver_id=%s", (north,))
    changed = ledger.update(manager, row["id"], edit(original, comment="Corrected invoice"))
    assert changed["approver_id"] == str(north) and changed["approver_name"] == "Northern manager"
    assert ledger.create(manager, original)["approver_name"] == "Northern manager"
    with pytest.raises(HTTPException, match="422"):
        ledger.create(manager, expense(venues[0], approver_id=north))


def test_pending_allocation_predating_directory_can_be_retried_and_completed(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    groups = FinanceAllocations(ledger.repo)
    old_request = AllocationCreate(**(command(venues).model_dump() | {"method": "pending"}))
    created = groups.create(owner, old_request, no_calculation)
    db.execute(
        "UPDATE finance_allocations a SET parts=(SELECT jsonb_agg(p-'approver_id'-'approver_name') "
        "FROM jsonb_array_elements(a.parts) p) WHERE id=%s", (created["id"],),
    )
    _, north, south = configure(db, venues)
    assert groups.create(owner, old_request, no_calculation)["id"] == created["id"]
    old = groups.pending(owner, DAY, DAY)["rows"][0]
    assert all(part.get("approver_name") is None for part in old["parts"])
    with pytest.raises(HTTPException, match="422"):
        groups.update(owner, created["id"], edited(old_request, method="equal"), calculate(owner))
    assert ledger.totals(owner, DAY, DAY)["pending_expenses"] == "100.01"
    parts = [
        {"department_id": venue, "approver_id": approver}
        for venue, approver in zip(venues, (north, south), strict=True)
    ]
    groups.update(owner, created["id"], edited(old_request, method="equal", parts=parts),
                  calculate(owner))
    assert ledger.totals(owner, DAY, DAY)["allocated_expenses"] == "100.01"
