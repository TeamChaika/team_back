"""Two independent checkpoints, one strictly sequential stream of iiko requests."""

import json
from contextlib import nullcontext
from datetime import date
from threading import Event
from uuid import UUID

import httpx
import pytest
from test_iiko_invoices import config
from test_iiko_writeoffs import document as writeoff_document
from test_invoice_history import snapshot as invoice_snapshot
from test_invoice_history import source as source
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db
from test_writeoff_history import captured as writeoff_snapshot

from app import sync_invoices as history
from app.sync_references import SyncError

START, END = date(2023, 3, 1), date(2023, 3, 3)


@pytest.fixture
def upstream(db, source, tmp_path, monkeypatch):
    control = {"calls": [], "fail": None, "stop": None}
    client_factory = httpx.Client

    def handle(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "connections":
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if path == "logout":
            control["calls"].append(("logout", None))
            return httpx.Response(200, json={"state": "logged_out"})
        day = request.url.params["date_from"]
        assert request.url.params["date_to"] == day
        assert request.url.params["revision_from"] == "-1"
        control["calls"].append((path, day))
        if control["fail"] == (path, day):
            return httpx.Response(409)
        return httpx.Response(200, json={})

    def capture(settings, source, resource, response, day):
        if control["stop"]:
            control["stop"].set()
        if resource == "writeoffs":
            return writeoff_snapshot(
                tmp_path, source, day, [writeoff_document(dateIncoming=f"{day}T23:00")]
            )
        return invoice_snapshot(tmp_path, source, day)

    monkeypatch.setattr(history.psycopg, "connect", lambda *a, **k: nullcontext(db))
    monkeypatch.setattr(history, "configured_sources", lambda settings: [source])
    monkeypatch.setattr(history, "capture_inventory", capture)
    monkeypatch.setattr(
        history.httpx,
        "Client",
        lambda **kwargs: client_factory(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return control


def requests(tmp_path, invoice_run=None, writeoff_run=None):
    return [
        history.HistoryRequest("writeoffs", tmp_path / "writeoffs.json", writeoff_run),
        history.HistoryRequest("incoming_invoices", tmp_path / "invoices.json", invoice_run),
    ]


def run(jobs, stop=None):
    return history.synchronize_histories(
        config(database_url="unused"), "http://127.0.0.1:8010", START, END, jobs, stop
    )


def test_alternates_from_independent_checkpoints_and_finishes_remaining_resource(
    db, source, tmp_path, upstream
):
    invoice_run, progress = history.prepare_run(db, source, START, END, None)
    history.commit_day(db, invoice_run, progress, invoice_snapshot(tmp_path, source, START))
    reports = run(requests(tmp_path, invoice_run))
    assert upstream["calls"] == [
        ("writeoffs", "2023-03-01"),
        ("incoming-invoices", "2023-03-02"),
        ("writeoffs", "2023-03-02"),
        ("incoming-invoices", "2023-03-03"),
        ("writeoffs", "2023-03-03"),
        ("logout", None),
    ]
    assert all(r["status"] == "succeeded" and r["logout_ok"] for r in reports)
    assert reports[1]["run_id"] == str(invoice_run)
    assert all(r["counts"]["completed_days"] == 3 for r in reports)
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 6


def test_failed_day_is_not_skipped_and_both_histories_resume(db, source, tmp_path, upstream):
    upstream["fail"] = ("writeoffs", "2023-03-02")
    with pytest.raises(SyncError, match="http_409"):
        run(requests(tmp_path))
    writeoff = json.loads((tmp_path / "writeoffs.json").read_text())
    invoice = json.loads((tmp_path / "invoices.json").read_text())
    for report in (writeoff, invoice):
        assert report["status"] == "failed" and report["logout_ok"]
        assert report["counts"]["next_date"] == "2023-03-02"
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 2
    upstream.update(fail=None, calls=[])
    run(requests(tmp_path, UUID(invoice["run_id"]), UUID(writeoff["run_id"])))
    assert upstream["calls"] == [
        ("writeoffs", "2023-03-02"),
        ("incoming-invoices", "2023-03-02"),
        ("writeoffs", "2023-03-03"),
        ("incoming-invoices", "2023-03-03"),
        ("logout", None),
    ]
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 6


def test_stop_commits_inflight_day_before_switching_resource(db, source, tmp_path, upstream):
    stop = Event()
    upstream["stop"] = stop
    with pytest.raises(SyncError, match="interrupted"):
        run(requests(tmp_path), stop)
    assert upstream["calls"] == [("writeoffs", "2023-03-01"), ("logout", None)]
    assert json.loads((tmp_path / "writeoffs.json").read_text())["counts"]["completed_days"] == 1
    assert json.loads((tmp_path / "invoices.json").read_text())["counts"]["completed_days"] == 0


def test_mirrored_progress_preserves_job_results_after_next_run(db, source, tmp_path, upstream):
    output, mirror = tmp_path / "job.json", tmp_path / "latest.json"
    prepared = []
    result = history.synchronize_histories(
        config(database_url="unused"),
        "http://127.0.0.1:8010",
        START,
        START,
        [history.HistoryRequest("incoming_invoices", output, mirror=mirror)],
        on_ready=lambda: prepared.append(True),
    )[0]
    assert prepared == [True]
    assert json.loads(mirror.read_text()) == json.loads(output.read_text()) == result
    assert mirror.stat().st_mode & 0o777 == 0o600
    history.synchronize_histories(
        config(database_url="unused"),
        "http://127.0.0.1:8010",
        START,
        START,
        [history.HistoryRequest("incoming_invoices", tmp_path / "next-job.json", mirror=mirror)],
    )
    assert json.loads(output.read_text()) == result
    assert json.loads(mirror.read_text())["run_id"] != result["run_id"]
