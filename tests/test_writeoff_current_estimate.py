"""Incomplete pending quotes can improve; approval freezes without deleting origin."""

# ruff: noqa: F811 -- pytest fixtures imported for this module.

from copy import deepcopy
from uuid import uuid4

import pytest

from app.documents import costs
from tests.test_writeoff_costs import (  # noqa: F401
    RECEIVER,
    SENDER,
    database,
    service,
    writeoff_body,
)


def stored(service, document_id):
    with service.database.connection() as db:
        return db.execute(
            "SELECT cost_estimate FROM writeoffs WHERE id=%s", (document_id,)
        ).fetchone()["cost_estimate"]


def confirm(service, doc):
    return service.dispatch(
        RECEIVER,
        "POST",
        f"writeoff/{doc['id']}/confirm",
        payload={"request_id": str(uuid4()), "version": doc["version"]},
    )


def quote(original, total):
    return {
        **deepcopy(original),
        "estimated_at": "2026-10-05T12:00:00+00:00",
        "total": total,
        "known_total": total or "0.00",
        "unpriced_count": 0 if total else 1,
    }


def test_incomplete_detail_improves_without_get_writes_and_approval_freezes(service, monkeypatch):
    created = service.dispatch(SENDER, "POST", "writeoff", payload=writeoff_body())
    original = stored(service, created["id"])
    current = quote(original, "2421.78")
    calls = []

    def estimate(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(current)

    monkeypatch.setattr(costs, "estimate", estimate)
    detail = service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")
    assert detail["cost_estimate"]["total"] == "2421.78"
    assert detail["cost_estimate"]["refreshed"] is True
    assert detail["cost_estimate"]["original_estimated_at"] == original["estimated_at"]
    assert detail["original_cost_estimate"] == original
    assert stored(service, created["id"]) == original
    assert service.dispatch(SENDER, "GET", "writeoff")["rows"][0]["cost_estimate"] == original
    assert len(calls) == 1  # Listing does not recalculate a page of recipes.

    approved = confirm(service, created)
    assert approved["cost_estimate"]["total"] == "2421.78"
    assert approved["cost_estimate"]["frozen_at_approval"] is True
    saved = stored(service, created["id"])
    assert {key: value for key, value in saved.items() if key != "approval_estimate"} == original
    assert saved["approval_estimate"] == approved["cost_estimate"]
    assert len(calls) == 2
    current["total"] = "9999.99"
    detail = service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")
    assert detail["cost_estimate"] == approved["cost_estimate"]
    assert detail["history"][0]["data"]["approval_estimate"] == approved["cost_estimate"]
    assert (
        service.dispatch(SENDER, "GET", "writeoff")["rows"][0]["cost_estimate"]
        == approved["cost_estimate"]
    )
    # Explicit failed delivery allows reapproval, but not a second price capture.
    with service.database.connection() as db:
        db.execute("UPDATE writeoffs SET submission_state='failed' WHERE id=%s", (created["id"],))
    retried = confirm(service, approved)
    assert retried["cost_estimate"] == approved["cost_estimate"]
    assert len(calls) == 2
    assert service.provider.sends == []


def test_approval_freezes_even_an_incomplete_current_quote(service, monkeypatch):
    created = service.dispatch(SENDER, "POST", "writeoff", payload=writeoff_body())
    original = stored(service, created["id"])
    monkeypatch.setattr(costs, "estimate", lambda *args, **kwargs: quote(original, None))
    approved = confirm(service, created)
    assert approved["cost_estimate"]["total"] is None
    assert approved["cost_estimate"]["frozen_at_approval"]
    monkeypatch.setattr(
        costs, "estimate", lambda *args, **kwargs: pytest.fail("Frozen quote repriced")
    )
    assert (
        service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")["cost_estimate"]
        == approved["cost_estimate"]
    )


@pytest.mark.parametrize("state", ["complete", "denied", "queued", "unknown", "legacy"])
def test_complete_or_processed_or_legacy_estimates_do_not_refresh(service, monkeypatch, state):
    from psycopg.types.json import Jsonb

    created = service.dispatch(SENDER, "POST", "writeoff", payload=writeoff_body())
    original = stored(service, created["id"])
    with service.database.connection() as db:
        if state == "complete":
            original = quote(original, "100.00")
            db.execute(
                "UPDATE writeoffs SET cost_estimate=%s WHERE id=%s",
                (Jsonb(original), created["id"]),
            )
        elif state == "denied":
            db.execute("UPDATE writeoffs SET status='Denied' WHERE id=%s", (created["id"],))
        elif state == "legacy":
            db.execute("UPDATE writeoffs SET cost_estimate=NULL WHERE id=%s", (created["id"],))
        else:
            db.execute(
                "UPDATE writeoffs SET submission_state=%s WHERE id=%s", (state, created["id"])
            )
    monkeypatch.setattr(
        costs, "estimate", lambda *args, **kwargs: pytest.fail("Unexpected refresh")
    )
    detail = service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")
    if state != "legacy":
        assert detail["cost_estimate"] == original
    else:
        assert stored(service, created["id"]) is None
    if state == "complete":
        assert confirm(service, created)["cost_estimate"] == original


@pytest.mark.parametrize("missing", [1, 2])
def test_approval_keeps_original_when_current_coverage_equal_or_worse(
    service, monkeypatch, missing
):
    from psycopg.types.json import Jsonb

    created = service.dispatch(SENDER, "POST", "writeoff", payload=writeoff_body())
    original = {**stored(service, created["id"]), "known_total": "100.00"}
    with service.database.connection() as db:
        db.execute(
            "UPDATE writeoffs SET cost_estimate=%s WHERE id=%s", (Jsonb(original), created["id"])
        )
    worse = {**quote(original, None), "unpriced_count": missing}
    monkeypatch.setattr(costs, "estimate", lambda *args, **kwargs: worse)
    assert (
        service.dispatch(RECEIVER, "GET", f"writeoff/{created['id']}")["cost_estimate"] == original
    )
    approved = confirm(service, created)
    assert approved["cost_estimate"] == {**original, "frozen_at_approval": True}
