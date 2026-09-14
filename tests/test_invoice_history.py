"""Transactional history checks against isolated PostgreSQL; no live iiko calls."""

import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from test_iiko_invoices import ITEM, config, document, wrap
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db

from app import sync_invoices as history
from app.services.iiko_invoices import read_incoming_invoices
from app.sync_references import Source, SyncError, register_sources

START = date(2023, 3, 1)
END = date(2023, 3, 3)


@pytest.fixture
def source(db, bundle):
    sources, _ = bundle
    register_sources(db, sources)
    db.execute("UPDATE chaika.sources SET server_type='CHAIN' WHERE id='primary'")
    return sources[0]


def snapshot(tmp_path, source, day, body=None):
    raw = wrap(document().replace("2026-09-09T22:35:47", f"{day}T22:35:47"))
    raw = raw if body is None else body
    path = tmp_path / f"{uuid4()}.xml"
    path.write_bytes(raw)
    items = [r.model_dump(mode="json") for r in read_incoming_invoices(path)]
    return {
        "id": str(uuid4()),
        "source_id": source.id,
        "resource": history.RESOURCE,
        "observed_at": datetime.now(UTC),
        "raw": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "payload": {
            "items": items,
            "total": len(items),
            "sync_business_date": day.isoformat(),
            "source_fingerprint": source.fingerprint,
        },
    }


def test_empty_day_and_resume_preserve_exact_data(db, source, tmp_path):
    run, progress = history.prepare_run(db, source, START, END, None)
    first = snapshot(tmp_path, source, START)
    progress = history.commit_day(db, run, progress, first)
    assert db.execute("SELECT sum FROM chaika.incoming_invoice_items").fetchone()[0] == Decimal(
        "101.123456789123456789"
    )
    assert (
        db.execute("SELECT first_seen_at FROM chaika.incoming_invoices").fetchone()[0]
        == first["observed_at"]
    )
    db.execute("UPDATE chaika.sync_runs SET status='failed',finished_at=now() WHERE id=%s", (run,))
    resumed_id, resumed = history.prepare_run(db, source, START, END, run)
    assert resumed_id == run and resumed["next_date"] == "2023-03-02"
    progress = history.commit_day(
        db, run, resumed, snapshot(tmp_path, source, date(2023, 3, 2), wrap(""))
    )
    assert progress["completed_days"] == 2 and progress["documents_read"] == 1
    assert db.execute("SELECT present_in_latest FROM chaika.incoming_invoice_items").fetchone()[0]
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 2


def test_line_changes_and_return_to_previous_state_keep_raw_history(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None)
    first = snapshot(tmp_path, source, START)
    state = history.commit_day(db, run, state, first)
    state = history.commit_day(
        db,
        run,
        state,
        snapshot(tmp_path, source, START + timedelta(days=1), wrap(document(items=""))),
    )
    assert (
        db.execute("SELECT present_in_latest FROM chaika.incoming_invoice_items").fetchone()[0]
        is False
    )
    history.commit_day(db, run, state, snapshot(tmp_path, source, END, first["raw"]))
    assert db.execute("SELECT count(*) FROM chaika.incoming_invoices").fetchone()[0] == 1
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.incoming_invoice_items WHERE present_in_latest"
        ).fetchone()[0]
        == 1
    )
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 3
    assert (
        db.execute("SELECT first_seen_at FROM chaika.incoming_invoices").fetchone()[0]
        == first["observed_at"]
    )


def test_duplicate_line_numbers_survive_reloads_and_removal(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None)
    raw = wrap(document(items=ITEM + ITEM.replace("101.123456789123456789", "25.5")))
    state = history.commit_day(db, run, state, snapshot(tmp_path, source, START, raw))
    state = history.commit_day(
        db, run, state, snapshot(tmp_path, source, START + timedelta(days=1), raw)
    )
    rows = db.execute(
        "SELECT num,num_occurrence,sum FROM chaika.incoming_invoice_items ORDER BY num_occurrence"
    ).fetchall()
    assert rows == [(1, 1, Decimal("101.123456789123456789")), (1, 2, Decimal("25.5"))]
    history.commit_day(db, run, state, snapshot(tmp_path, source, END))
    assert db.execute(
        "SELECT num_occurrence,present_in_latest FROM chaika.incoming_invoice_items "
        "ORDER BY num_occurrence"
    ).fetchall() == [(1, True), (2, False)]


@pytest.mark.parametrize("failure", ["bad_line", "checkpoint_conflict"])
def test_failed_day_rolls_back_raw_headers_lines_and_checkpoint(db, source, tmp_path, failure):
    run, state = history.prepare_run(db, source, START, END, None)
    data = snapshot(tmp_path, source, START)
    if failure == "bad_line":
        data["payload"]["items"][0]["items"][0]["sum"] = None
        expected = psycopg.errors.NotNullViolation
    else:
        db.execute(
            "UPDATE chaika.sync_runs SET counts=jsonb_set(counts,'{next_date}','\"2023-03-02\"') "
            "WHERE id=%s",
            (run,),
        )
        expected = SyncError
    with pytest.raises(expected):
        history.commit_day(db, run, state, data)
    for table in ("raw_snapshots", "incoming_invoices", "incoming_invoice_items"):
        assert db.execute(f"SELECT count(*) FROM chaika.{table}").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT counts->>'completed_days' FROM chaika.sync_runs WHERE id=%s", (run,)
        ).fetchone()[0]
        == "0"
    )


def test_resume_rejects_different_source_and_range(db, source):
    run, _ = history.prepare_run(db, source, START, END, None)
    with pytest.raises(SyncError, match="scope_mismatch"):
        history.prepare_run(db, source, START, END + timedelta(days=1), run)
    with pytest.raises(SyncError, match="scope_mismatch"):
        history.prepare_run(
            db, Source(source.id, source.label, source.base_url, "b" * 64), START, END, run
        )


def test_wrong_snapshot_scope_does_not_advance(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None)
    with pytest.raises(SyncError, match="snapshot_scope_mismatch"):
        history.commit_day(db, run, state, snapshot(tmp_path, source, END))


def test_failure_logout_and_resume_skip_completed_day(db, source, tmp_path, monkeypatch):
    requests = []
    fail = True
    client_factory = httpx.Client

    def handle(request):
        nonlocal fail
        requests.append(request)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, json={"state": "logged_out"})
        assert request.url.path == "/api/v1/iiko/incoming-invoices"
        day = request.url.params["date_from"]
        assert day == request.url.params["date_to"]
        assert request.url.params["revision_from"] == "-1"
        if day == "2023-03-02" and fail:
            return httpx.Response(409)
        return httpx.Response(200, json={"day": day})

    monkeypatch.setattr(history.psycopg, "connect", lambda *a, **k: nullcontext(db))
    monkeypatch.setattr(history, "configured_sources", lambda settings: [source])
    monkeypatch.setattr(
        history.httpx,
        "Client",
        lambda **kwargs: client_factory(**kwargs, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(
        history,
        "capture_inventory",
        lambda settings, source, resource, response, day: snapshot(tmp_path, source, day),
    )
    output = tmp_path / "progress.json"
    with pytest.raises(SyncError, match="http_409"):
        history.synchronize_invoices(
            config(database_url="unused"), "http://127.0.0.1:8010", START, END, output
        )
    report = json.loads(output.read_text())
    assert report["logout_ok"] is True and report["counts"]["completed_days"] == 1
    assert requests[-1].url.path.endswith("/logout")
    fail, requests = False, []
    result = history.synchronize_invoices(
        config(database_url="unused"),
        "http://127.0.0.1:8010",
        START,
        END,
        output,
        UUID(report["run_id"]),
    )
    assert result["status"] == "succeeded" and result["counts"]["completed_days"] == 3
    assert [
        r.url.params["date_from"] for r in requests if r.url.path.endswith("/incoming-invoices")
    ] == ["2023-03-02", "2023-03-03"]
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 3
    assert output.stat().st_mode & 0o777 == 0o600


def test_fetch_retries_same_day_and_does_not_skip():
    calls = []
    stop = Event()
    stop.wait = lambda timeout: False

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    with httpx.Client(
        base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle)
    ) as client:
        assert history.fetch_day(client, START, stop) == {"ok": True}
        assert len(calls) == 3 and len(set(calls)) == 1
        stop.set()
        with pytest.raises(SyncError, match="interrupted"):
            history.fetch_day(client, START, stop)


def test_capture_uses_raw_precision_and_rejects_corruption(tmp_path, monkeypatch):
    from app import sync_inventory
    from app.schemas.iiko_invoices import IncomingInvoicesQuery, IncomingInvoicesResponse
    from app.sync_inventory import capture_inventory
    from app.sync_references import configured_sources

    settings = config()
    source = configured_sources(settings)[0]
    data = snapshot(tmp_path, source, START)
    folder = tmp_path / ".local/incoming-invoices"
    folder.mkdir(parents=True)
    path = folder / f"{data['id']}.xml"
    path.write_bytes(data["raw"])
    response = IncomingInvoicesResponse(
        snapshot_id=data["id"],
        received_at=data["observed_at"],
        request=IncomingInvoicesQuery(date_from=START, date_to=START),
        total=1,
        items_count=1,
        documents=read_incoming_invoices(path),
        source_bytes=len(data["raw"]),
        sha256=data["sha256"],
    ).model_dump(mode="json")
    (folder / f"{data['id']}.meta.json").write_text(
        json.dumps({"source_fingerprint": source.fingerprint})
    )
    monkeypatch.setattr(sync_inventory, "BACKEND_DIR", tmp_path)
    result = capture_inventory(settings, source, history.RESOURCE, response, START)
    assert result["payload"]["items"][0]["items"][0]["sum"] == "101.123456789123456789"
    damaged = deepcopy(response)
    damaged["sha256"] = "0" * 64
    with pytest.raises(SyncError, match="raw_mismatch"):
        capture_inventory(settings, source, history.RESOURCE, damaged, START)
