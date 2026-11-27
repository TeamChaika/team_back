"""Exercise catalog changes against posted and deferred fictional expenses."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_finance_allocations import DAY, calculate, command, edited, no_calculation
from test_finance_conversion import conversion
from test_finance_ledger import edit, expense
from test_finance_ledger import ledger_db as ledger_db

from app.web.finance_allocations import FinanceAllocations


def test_expense_keeps_label_and_report_groups_both_names_under_one_code(ledger_db):
    ledger, db, (manager, _, _, owner), venues = ledger_db
    code = str(uuid4())
    db.execute("INSERT INTO finance_categories(code,name) VALUES (%s,'Cleaning')", (code,))
    request = expense(venues[0], category=code)
    older = ledger.create(manager, request)
    assert older["category_name"] == "Cleaning"
    db.execute("UPDATE finance_categories SET name='Cleaning services' WHERE code=%s", (code,))
    newer = ledger.create(manager, expense(venues[0], category=code))
    assert newer["category_name"] == "Cleaning services"
    db.execute("UPDATE finance_categories SET archived=true WHERE code=%s", (code,))
    for invalid in (code, str(uuid4())):
        with pytest.raises(HTTPException, match="422"):
            ledger.create(manager, expense(venues[0], category=invalid))
    assert ledger.create(manager, request)["id"] == older["id"]
    checked = ledger.update(manager, older["id"], edit(request, comment="Checked"))
    assert checked["category_name"] == "Cleaning"
    assert ledger.history(owner, older["id"])["rows"][0]["after"]["category_name"] == "Cleaning"
    rows = ledger.totals(owner, DAY, DAY)["by_category"]
    assert rows == [{"category": code, "category_name": "Cleaning services", "amount": "241.00"}]
    changed = ledger.update(manager, older["id"], edit(request, revision=2, category="rent"))
    assert changed["category_name"] == "Аренда"
    with pytest.raises(HTTPException, match="422"):
        ledger.update(manager, older["id"], edit(request, revision=3))


def test_pending_invoice_posts_shares_with_its_original_category_label(ledger_db):
    ledger, db, (_, _, _, owner), venues = ledger_db
    groups = FinanceAllocations(ledger.repo)
    request = command(venues).model_copy(update={"method": "pending"})
    pending = groups.create(owner, request, no_calculation)
    assert pending["category_name"] == "Аренда"
    db.execute("UPDATE finance_categories SET name='Building rent',archived=true WHERE code='rent'")
    assert groups.create(owner, request, no_calculation)["id"] == pending["id"]
    assert groups.pending(owner, DAY, DAY)["rows"][0]["category_name"] == "Аренда"
    assert ledger.totals(owner, DAY, DAY)["by_category"] == [
        {"category": "rent", "category_name": "Building rent", "amount": "100.01"}
    ]
    with pytest.raises(HTTPException, match="422"):
        groups.create(owner, command(venues), calculate(owner))
    posted = groups.update(owner, pending["id"], edited(request, method="equal"), calculate(owner))
    assert posted["category_name"] == "Аренда"
    shares = ledger.list(owner, DAY, DAY, limit=10, offset=0)["rows"]
    assert len(shares) == 2 and {row["category_name"] for row in shares} == {"Аренда"}
    assert groups.history(owner, posted["id"])["rows"][0]["after"]["category_name"] == "Аренда"


def test_archived_last_approver_keeps_approval_required_and_historical_name(ledger_db):
    ledger, db, (manager, _, _, _), venues = ledger_db
    person = uuid4()
    db.execute("INSERT INTO finance_approvers(id,name,available_to_all) VALUES(%s,%s,true)",
               (person, "Supervisor"))
    request = expense(venues[0], approver_id=person)
    row = ledger.create(manager, request)
    db.execute("UPDATE finance_approvers SET archived=true WHERE id=%s", (person,))
    assert ledger.approvers(manager) == {"rows": [], "required": True}
    for selection in (None, person):
        with pytest.raises(HTTPException, match="422"):
            ledger.create(manager, expense(venues[0], approver_id=selection))
    assert ledger.update(manager, row["id"], edit(request))["approver_name"] == "Supervisor"


def test_converting_a_legacy_expense_after_archive_inherits_its_category(ledger_db):
    ledger, db, (manager, _, _, owner), venues = ledger_db
    request = expense(venues[0])
    original = ledger.create(manager, request)
    db.execute("UPDATE finance_expense_history SET after_state=after_state-'category_name' "
               "WHERE expense_id=%s", (original["id"],))
    db.execute("UPDATE finance_categories SET name='Renamed rent',archived=true WHERE code='rent'")
    assert ledger.create(manager, request)["category_name"] == "Аренда"
    groups = FinanceAllocations(ledger.repo)
    allocation = conversion(original, venues)
    result = groups.convert(owner, original["id"], allocation, calculate(owner))
    assert result["category_name"] == "Аренда"
    assert groups.convert(owner, original["id"], allocation, no_calculation)["id"] == result["id"]
    journal = ledger.list(owner, DAY, DAY, limit=10, offset=0)["rows"]
    assert len(journal) == 3 and {r["category_name"] for r in journal} == {"Аренда"}
    assert ledger.totals(owner, DAY, DAY)["allocated_expenses"] == "120.50"
