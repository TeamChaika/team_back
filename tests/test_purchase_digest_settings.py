"""The purchase dispatch can be stopped without Telegram, and edits preserve the cutoff."""

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.web import purchase_digest as api
from tests.test_low_markup_postgres import ACTOR, VENUE
from tests.test_low_markup_postgres import database as database
from tests.test_low_markup_settings import Administration


@pytest.mark.parametrize(("minute", "expected"), [(59, date(2026, 1, 12)), (60, date(2026, 1, 13))])
def test_activation_cutoff(minute, expected):
    instant = datetime(2026, 1, 12, 5 + minute // 60, minute % 60, tzinfo=UTC)
    assert api.next_report_date(instant) == expected


def test_enable_change_and_disable(database, monkeypatch):
    db, _ = database
    migration = (
        Path(__file__).parents[1] / "supabase/migrations/20261125010000_purchase_digest_reports.sql"
    )
    db.execute(migration.read_text(encoding="utf-8"))
    administration = Administration(db)
    verify = Mock(return_value=("123", "Kitchen"))
    monkeypatch.setattr(api, "_verified_chat", verify)

    def save(revision, chat, enabled):
        rows = api.save_destination(
            administration,
            ACTOR,
            VENUE,
            api.DestinationWrite(
                chat_id=chat,
                enabled=enabled,
                revision=revision,
            ),
        )["departments"]
        return next(row for row in rows if row["id"] == str(VENUE))

    assert save(0, "-123", True)["chat_title"] == "Kitchen"
    assert verify.call_count == 1
    with pytest.raises(HTTPException) as stale:
        save(0, "-456", True)
    assert stale.value.status_code == 409
    assert verify.call_count == 1
    db.execute("UPDATE chaika.purchase_digest_destinations SET start_date='2025-01-01'")
    assert save(1, "-123", True)["start_date"] == "2025-01-01"
    assert save(2, "-456", True)["start_date"] != "2025-01-01"
    verify.side_effect = AssertionError("Disabling an existing route must work during an outage")
    assert save(3, "-456", False)["enabled"] is False

    db.execute(
        "INSERT INTO chaika.operation_settings VALUES(true,1,%s,now(),%s)",
        (Jsonb({"analytics": {"archived_department_ids": [str(VENUE)]}}), ACTOR),
    )
    with pytest.raises(HTTPException) as archived:
        save(4, "-456", True)
    assert archived.value.status_code == 422
