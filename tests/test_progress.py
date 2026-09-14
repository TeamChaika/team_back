import json

from fastapi.testclient import TestClient

from app import progress


def report(status="running"):
    return {
        "run_id": "example",
        "status": status,
        "updated_at": "2026-09-10T19:00:00+00:00",
        "counts": {
            "completed_days": 10,
            "total_days": 20,
            "documents_read": 50,
            "items_read": 70,
            "date_from": "2023-03-01",
            "date_to": "2023-03-20",
            "source_fingerprint": "private",
        },
        "private": "secret",
    }


def test_projection_does_not_expose_private_fields():
    result = progress.describe("invoices", report(), None, True)
    assert result["percent"] == 50 and result["documents"] == 50
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)


def test_dead_process_does_not_look_running():
    assert progress.describe("invoices", report(), None, False)["status"] == "interrupted"
    assert (
        progress.describe("invoices", report("succeeded"), None, False)["status"] == "interrupted"
    )


def test_cash_shift_history_is_visible_and_dead_process_is_detected(tmp_path, monkeypatch):
    (tmp_path / "cash-shifts-latest.json").write_text(json.dumps(report()))
    (tmp_path / "cash-shifts-process.json").write_text(json.dumps({"pid": 321}))
    monkeypatch.setattr(progress, "process_alive", lambda record, resource: False)
    item = progress.progress_snapshot(tmp_path)["jobs"][0]
    assert item["resource"] == "cash_shifts" and item["mode"] == "cash_shift_history"
    assert item["title"] == "Кассовые смены" and item["percent"] == 50
    assert item["status"] == "interrupted"


def test_waiting_and_failed_prerequisite(tmp_path, monkeypatch):
    (tmp_path / "invoices-latest.json").write_text(json.dumps(report("failed")))
    (tmp_path / "writeoffs-process.json").write_text(
        json.dumps(
            {
                "pid": 123,
                "after_invoice_run": "example",
                "date_from": "2023-03-01",
                "date_to": "2023-03-20",
            }
        )
    )
    monkeypatch.setattr(progress, "process_alive", lambda record, resource: resource == "writeoffs")
    result = progress.progress_snapshot(tmp_path)
    assert result["jobs"][1]["status"] == "blocked"
    assert result["jobs"][1]["total_days"] == 20


def test_sales_history_is_visible_without_exposing_private_details(tmp_path, monkeypatch):
    (tmp_path / "sales-latest.json").write_text(json.dumps(report()))
    (tmp_path / "sales-process.json").write_text(json.dumps({"pid": 321}))
    monkeypatch.setattr(progress, "process_alive", lambda record, resource: True)
    item = progress.progress_snapshot(tmp_path)["jobs"][0]
    assert item["resource"] == "sales" and item["mode"] == "sales_history"
    assert item["title"] == "Продажи OLAP · 7 отчётов"
    assert item["percent"] == 50 and item["status"] == "running"
    assert "private" not in json.dumps(item)


def test_view_and_status_api_report_errors_without_faking_zero(monkeypatch):
    with TestClient(progress.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/not-allowed").status_code == 404
        monkeypatch.setattr(progress, "progress_snapshot", lambda: {"jobs": []})
        assert client.get("/api/v1/sync/status").json() == {"jobs": []}

        def broken():
            raise OSError("secret-path")

        monkeypatch.setattr(progress, "progress_snapshot", broken)
        response = client.get("/api/v1/sync/status")
        assert response.status_code == 503 and "secret-path" not in response.text


def test_sales_partial_day_is_visible(tmp_path, monkeypatch):
    payload = {**report(), "partial_day": "2026-09-13", "partial_as_of": "2026-09-13T13:00:00Z"}
    (tmp_path / "sales-latest.json").write_text(json.dumps(payload))
    monkeypatch.setattr(progress, "process_alive", lambda *args: True)
    item = progress.progress_snapshot(tmp_path)["jobs"][0]
    assert item["partial_day"] == "2026-09-13"
    assert item["partial_as_of"] == "2026-09-13T13:00:00Z"
