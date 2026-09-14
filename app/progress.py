"""Local read-only progress screen. Run separately to avoid restarting the collector."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import BACKEND_DIR

STATIC = Path(__file__).parent / "static/progress"
SYNC = BACKEND_DIR / ".local/sync"
LABELS = {
    "invoices": "Приходные накладные",
    "writeoffs": "Акты списания",
    "outgoing": "Расходные накладные",
    "transfers": "Внутренние перемещения",
}


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError("invalid_progress_file")
    return data


def process_alive(record: dict | None, resource: str) -> bool:
    if not record or not isinstance(record.get("pid"), int) or record["pid"] <= 0:
        return False
    result = subprocess.run(
        ["ps", "-p", str(record["pid"]), "-o", "stat=,command="],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    fields = result.stdout.strip().split(None, 1)
    return bool(
        len(fields) == 2
        and not fields[0].startswith("Z")
        and any(
            module in fields[1]
            for module in (
                f"-m app.sync_{resource}",
                "-m app.sync_documents",
                "-m app.sync_refresh",
            )
        )
    )


def describe(resource: str, report: dict | None, record: dict | None, alive: bool) -> dict:
    counts = report.get("counts", {}) if report else {}
    record = record or {}
    done, total = int(counts.get("completed_days", 0)), int(counts.get("total_days", 0))
    start = counts.get("date_from", record.get("date_from"))
    end = counts.get("date_to", record.get("date_to"))
    if not total and start and end:
        total = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1
    state = report.get("status", "not_started") if report else "not_started"
    if state == "running" and not alive:
        state = "interrupted"
    if state == "succeeded" and (total <= 0 or done != total):
        state = "interrupted"
    if report is None and record.get("after_invoice_run"):
        state = "waiting" if alive else "interrupted"
    return {
        "resource": resource,
        "mode": "rolling_60_days" if record.get("mode") == "rolling_60_days" else "daily_history",
        "title": LABELS.get(resource, "События RMS"),
        "status": state,
        "run_id": report.get("run_id") if report else None,
        "date_from": start,
        "date_to": end,
        "completed_days": done,
        "total_days": total,
        "percent": round(min(100, max(0, done / total * 100)), 1) if total else 0,
        "documents": int(counts.get("documents_read", 0)),
        "items": int(counts.get("items_read", 0)),
        "warning_days": int(counts.get("warning_days", 0)),
        "completed_through": counts.get("completed_through"),
        "next_date": counts.get("next_date") if state != "succeeded" else None,
        "updated_at": report.get("updated_at") if report else None,
        "error_code": report.get("error_code") if report else None,
    }


def progress_snapshot(directory: Path = SYNC) -> dict:
    jobs = []
    for resource in LABELS:
        report = read_json(directory / f"{resource}-latest.json")
        record = read_json(directory / f"{resource}-process.json")
        jobs.append(describe(resource, report, record, process_alive(record, resource)))
    if jobs[1]["status"] == "waiting":
        if jobs[0]["status"] in {"failed", "interrupted"}:
            jobs[1]["status"] = "blocked"
        elif jobs[0]["status"] == "succeeded":
            jobs[1]["status"] = "starting"
    events = read_json(directory / "events-history-latest.json")
    if events:
        record = read_json(directory / "events-history-process.json")
        alive = process_alive(record, "event_history")
        for source in events["sources"]:
            job = describe("events", source, record, alive)
            if job["status"] == "waiting" and not alive:
                job["status"] = "interrupted"
            job.update(
                {
                    "resource": f"events-{source['source_id']}",
                    "mode": "events_history",
                    "title": f"События — {source['label']}",
                    "documents": int(source["counts"].get("events_read", 0)),
                    "items": int(source["counts"].get("transfer_events_matched", 0)) // 2,
                    "partial_day": source.get("partial_day"),
                    "partial_as_of": source.get("partial_as_of"),
                }
            )
            jobs.append(job)
    cash_shifts = read_json(directory / "cash-shifts-latest.json")
    if cash_shifts:
        record = read_json(directory / "cash-shifts-process.json")
        job = describe(
            "cash_shifts", cash_shifts, record, process_alive(record, "cash_shift_history")
        )
        job.update(
            title="Кассовые смены",
            mode="cash_shift_history",
            partial_day=cash_shifts.get("partial_day"),
            partial_as_of=cash_shifts.get("partial_as_of"),
        )
        jobs.insert(0, job)
    sales = read_json(directory / "sales-latest.json")
    if sales:
        record = read_json(directory / "sales-process.json")
        job = describe("sales", sales, record, process_alive(record, "sales_history"))
        job.update(
            title="Продажи OLAP · 7 отчётов",
            mode="sales_history",
            partial_day=sales.get("partial_day"),
            partial_as_of=sales.get("partial_as_of"),
        )
        jobs.insert(0, job)
    return {"checked_at": datetime.now(UTC).isoformat(), "jobs": jobs}


app = FastAPI(title="Chaika — прогресс синхронизации", version="1.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.get("/api/v1/sync/status", tags=["Синхронизация"], summary="Текущий прогресс загрузчиков")
def sync_status():
    try:
        payload = progress_snapshot()
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise HTTPException(503, "Не удалось прочитать текущий прогресс") from None
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/assets/{name}", include_in_schema=False)
def asset(name: str):
    if name not in {"app.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})
