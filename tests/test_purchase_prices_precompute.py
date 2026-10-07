"""Prepared prices must match source reads across live authorization and corrections."""

from datetime import date
from itertools import product
from uuid import uuid4

import pytest
from psycopg.rows import dict_row, tuple_row
from test_inventory_sync import inventory as inventory
from test_invoice_history import snapshot
from test_invoice_history import source as source
from test_purchase_prices import data as data
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db

from app import sync_invoices as history
from app.purchase_prices_precompute import publish_prepared, source_revision
from app.sync_references import append_snapshot
from app.web.purchase_prices import HOUSEHOLD_GROUP_ID, read_purchase_prices
from app.web.purchase_prices_prepared import prepared_receipts_query
from app.web.repository import Repository, Scope


@pytest.fixture
def prepared_db(db, data):
    db.row_factory = dict_row
    return db


def read(db, stores=(), *, owner=False, **options):
    scope = Scope({"role": "owner" if owner else "manager"}, (), None, tuple(stores), ())
    repo = object.__new__(Repository)
    report = read_purchase_prices(
        db,
        scope,
        repo.resource_query(scope, "invoices"),
        as_of=date(2026, 9, 13),
        **options,
    )
    # Legacy list sorting has no unit/link tie breaker; compare identical facts
    # independently of PostgreSQL's unspecified order inside those ties.
    report["rows"].sort(
        key=lambda row: (str(row["product_id"]), str(row["unit_id"]), row["linked"])
    )
    return report


def publish(db):
    return publish_prepared(db, source_revision(db))


def test_exact_parity_across_overlapping_scopes_units_linkage_and_recency(prepared_db, data):
    receipt, _, a, unit, b = data
    hidden, other_unit = uuid4(), uuid4()
    for day in range(1, 13):
        for store, linked, value in product((a, b, hidden), (False, True), ("100", "130")):
            receipt(f"2026-08-{day:02d}", amount=str(day), value=value, store=store, linked=linked)
    receipt("2026-09-01", "2", "500", store=a)
    receipt("2026-09-01T00:00:00", "3", "1000", store=b)
    receipt("2026-09-01", "1", "700", unit=other_unit)
    receipt("2023-01-01", product=uuid4())
    future = uuid4()
    receipt("2026-09-12", product=future)
    receipt("2026-09-14", product=future)
    options = [
        dict(stores=stores, owner=owner, kind=kind, recent_only=recent)
        for stores, owner in [
            ((a,), False),
            ((b,), False),
            ((a, b), False),
            ((b, a), False),
            ((), False),
            ((), True),
        ]
        for kind, recent in product(("all", "linked", "unlinked"), (False, True))
    ]
    expected = [read(prepared_db, **option) for option in options]
    publish(prepared_db)
    assert [read(prepared_db, **option) for option in options] == expected
    largest = prepared_db.execute(
        "SELECT max(n) AS n FROM (SELECT count(*) AS n "
        "FROM chaika.purchase_prices_prepared_receipts "
        "GROUP BY required_store_ids,has_unknown_store,product_id,unit_id,linked) x"
    ).fetchone()["n"]
    assert largest == 7
    # Details still contain original documents, with an identical latest price.
    current = read(prepared_db, (a, b), kind="all")["rows"]
    selected = next(row for row in current if row["unit_id"] == unit and not row["linked"])
    detail = read(prepared_db, (a, b), selection=(selected["product_id"], None, unit, False))
    assert detail["rows"][0]["current"]["price"] == selected["current"]["price"]
    assert len(detail["rows"][0]["current"]["lines"]) == 2


def test_all_present_lines_control_authorization_even_when_not_priced(prepared_db, data):
    receipt, _, a, _, hidden = data
    valid = receipt("2026-09-01", value="123")
    for excluded in (dict(expense=True), dict(product=None)):
        mixed = receipt("2026-09-10", value="9999")
        receipt("2026-09-10", document=mixed, num=2, store=hidden, **excluded)
    unknown = receipt("2026-09-11", store=None, value="8888")
    # Explicit authorized line overrides an inaccessible default, as in the guard.
    explicit = receipt("2026-09-02", value="140")
    prepared_db.execute(
        "UPDATE chaika.incoming_invoices SET default_store_id=%s WHERE id=%s", (hidden, explicit)
    )
    # A removed hidden line cannot deny access; a null explicit line uses the default.
    receipt("2026-09-01", document=valid, num=2, store=hidden, present=False)
    inherited = receipt("2026-09-03", value="150")
    prepared_db.execute(
        "UPDATE chaika.incoming_invoice_items SET store_id=NULL WHERE document_id=%s", (inherited,)
    )
    before = [
        read(prepared_db, (a,)),
        read(prepared_db, (a, hidden)),
        read(prepared_db, owner=True),
        read(prepared_db),
    ]
    publish(prepared_db)
    after = [
        read(prepared_db, (a,)),
        read(prepared_db, (a, hidden)),
        read(prepared_db, owner=True),
        read(prepared_db),
    ]
    assert after == before
    assert after[0]["rows"][0]["current"]["price"] == 150
    assert after[2]["rows"][0]["current"]["price"] == 8888
    assert after[3]["rows"] == []
    assert unknown != valid


def test_invalid_history_and_null_units_remain_in_quality_counts(prepared_db, data):
    receipt, _, a, _, b = data
    for day in range(1, 10):
        receipt(f"2026-08-{day:02d}", value="100")
    receipt("2026-09-01", amount="0", store=b)
    receipt("2026-09-02", value="120")
    for amount in (None, "-1", "0"):
        receipt("2026-09-03", amount=amount, product=uuid4())
    receipt("2026-09-03", unit=None)
    receipt("2026-09-03", value="-1", product=uuid4())
    before = read(prepared_db, (a, b))
    assert before["stats"]["invalid"] == 6
    publish(prepared_db)
    assert read(prepared_db, (a, b)) == before
    # A narrower live scope excludes the invalid observation in b.
    assert read(prepared_db, (a,))["stats"]["invalid"] == 5


def test_household_classification_and_labels_are_live(prepared_db, data):
    receipt, _, a, _, _ = data
    receipt("2026-09-01")
    publish(prepared_db)
    row = read(prepared_db, (a,))["rows"][0]
    prepared_db.execute(
        "INSERT INTO chaika.product_groups SELECT source_id,%s,'Household',NULL,"
        "code,num,deleted,details,present_in_latest,first_seen_at,last_seen_at,last_snapshot_id "
        "FROM chaika.product_groups LIMIT 1",
        (HOUSEHOLD_GROUP_ID,),
    )
    prepared_db.execute(
        "UPDATE chaika.products SET name='Renamed after preparation',group_id=%s WHERE id=%s",
        (HOUSEHOLD_GROUP_ID, row["product_id"]),
    )
    assert read(prepared_db, (a,))["rows"] == []
    unfiltered = read(prepared_db, (a,), exclude_household=False)["rows"]
    assert unfiltered[0]["product"] == "Renamed after preparation"


def test_successful_inventory_correction_invalidates_then_rebuilds(prepared_db, data, inventory):
    receipt, _, a, _, _ = data
    old = receipt("2026-09-01", value="100")
    receipt("2026-09-02", value="120")
    scope = Scope({"role": "manager"}, (), None, (a,), ())
    assert prepared_receipts_query(prepared_db, scope, "all") is None
    publish(prepared_db)
    assert prepared_receipts_query(prepared_db, scope, "all") is not None
    original_revision = source_revision(prepared_db)
    for job, status in (
        ("references", "succeeded"),
        ("dictionaries", "succeeded"),
        ("inventory", "failed"),
        ("inventory", "running"),
    ):
        prepared_db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status,finished_at) "
            "VALUES (%s,%s,%s,CASE WHEN %s='running' THEN NULL ELSE now() END)",
            (uuid4(), job, status, status),
        )
    assert source_revision(prepared_db) == original_revision
    run_id = uuid4()
    prepared_db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES (%s,'inventory','running')", (run_id,)
    )
    # Full inventory captures raw data before its final publication transaction.
    # Preparing during that phase must still invalidate when receipt rows commit.
    append_snapshot(prepared_db, run_id, dict(inventory[2]["incoming_invoices"], id=str(uuid4())))
    assert source_revision(prepared_db) == original_revision
    publish(prepared_db)
    assert prepared_receipts_query(prepared_db, scope, "all") is not None
    with prepared_db.transaction():
        prepared_db.execute(
            "UPDATE chaika.incoming_invoice_items SET sum=80 WHERE document_id=%s", (old,)
        )
        prepared_db.execute(
            "UPDATE chaika.sync_runs SET status='succeeded',finished_at=now() WHERE id=%s",
            (run_id,),
        )
    assert prepared_receipts_query(prepared_db, scope, "all") is None
    corrected = read(prepared_db, (a,))
    assert corrected["rows"][0]["previous"]["price"] == 80
    publish(prepared_db)
    assert prepared_receipts_query(prepared_db, scope, "all") is not None
    assert read(prepared_db, (a,)) == corrected


def test_interrupted_publication_rolls_back_metadata_and_all_receipts(prepared_db, data):
    receipt, _, a, _, _ = data
    receipt("2026-09-01")
    original = publish(prepared_db)
    expected = read(prepared_db, (a,))

    class StopAfterBuild:
        calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls > 1

    with pytest.raises(RuntimeError, match="preparation_interrupted"):
        publish_prepared(prepared_db, source_revision(prepared_db), stop=StopAfterBuild())
    current = prepared_db.execute("SELECT id FROM chaika.purchase_prices_prepared").fetchone()
    assert str(current["id"]) == original["generation_id"]
    assert read(prepared_db, (a,)) == expected


def test_unmigrated_database_uses_legacy_reader():
    class Unmigrated:
        def execute(self, query):
            assert "to_regclass" in query
            return self

        def fetchone(self):
            return {"available": False}

    assert prepared_receipts_query(Unmigrated(), None, "all") is None


def test_null_timestamp_preserves_legacy_observation(prepared_db, data):
    receipt, _, a, _, _ = data
    receipt(None)
    expected = read(prepared_db, (a,), recent_only=False)
    publish(prepared_db)
    assert read(prepared_db, (a,), recent_only=False) == expected
    assert read(prepared_db, (a,), recent_only=True)["rows"] == []


def test_committed_history_checkpoint_invalidates_running_or_failed_run(db, source, tmp_path):
    start, end = date(2026, 9, 1), date(2026, 9, 3)
    run_id, progress = history.prepare_run(db, source, start, end, None)
    db.row_factory = dict_row
    scope = Scope({"role": "owner"}, (), None, (), ())
    publish(db)
    # Merely creating/capturing a history request has not published receipts yet.
    first = snapshot(tmp_path, source, start)
    assert prepared_receipts_query(db, scope, "all") is not None
    progress = history.commit_day(db, run_id, progress, first)
    assert prepared_receipts_query(db, scope, "all") is None
    running_revision = source_revision(db)
    db.execute(
        "UPDATE chaika.sync_runs SET status='failed',finished_at=now() WHERE id=%s", (run_id,)
    )
    assert source_revision(db) == running_revision
    assert prepared_receipts_query(db, scope, "all") is None
    expected = read(db, owner=True)
    publish(db)
    assert read(db, owner=True) == expected
    db.row_factory = tuple_row
    _, progress = history.prepare_run(db, source, start, end, run_id)
    db.row_factory = dict_row
    assert prepared_receipts_query(db, scope, "all") is not None
    history.commit_day(db, run_id, progress, snapshot(tmp_path, source, date(2026, 9, 2)))
    assert prepared_receipts_query(db, scope, "all") is None
