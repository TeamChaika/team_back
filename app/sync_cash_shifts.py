"""Complete opening days of cash shifts, with immutable RAW and explicit POS evidence."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import httpx
import psycopg
from defusedxml import ElementTree
from psycopg import sql
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.schemas.iiko_cash_shifts import CashShift, CashShiftsQuery, CashShiftsResponse
from app.schemas.sync_jobs import CashShiftSyncQuery
from app.services.iiko_cash_shifts import read_cash_shifts
from app.sync_references import (
    Source,
    SyncError,
    append_snapshot,
    capture_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)


def capture_cash_shifts(settings, source: Source, query, response, directory: Path | None = None):
    folder = directory if directory is not None else BACKEND_DIR / ".local/cash-shifts"
    key = str(UUID(response["snapshot_id"]))
    metadata = json.loads((folder / f"{key}.meta.json").read_text())
    if metadata.pop("source_fingerprint") != source.fingerprint:
        raise SyncError("cash_shifts_source_mismatch")
    if metadata.pop("source_endpoint") != "v2/cashshifts/list":
        raise SyncError("cash_shifts_endpoint_mismatch")
    if metadata != {k: v for k, v in response.items() if k != "items"}:
        raise SyncError("cash_shifts_snapshot_mismatch")
    parsed = CashShiftsResponse.model_validate(response, by_name=True)
    if parsed.request != query:
        raise SyncError("cash_shifts_scope_mismatch")
    path = folder / f"{key}.json"
    if path.stat().st_size > settings.iiko_cash_shifts_max_response_bytes:
        raise SyncError("cash_shifts_raw_too_large")
    raw = path.read_bytes()
    if len(raw) != parsed.source_bytes or hashlib.sha256(raw).hexdigest() != parsed.sha256:
        raise SyncError("cash_shifts_raw_mismatch")
    rows = read_cash_shifts(path)
    if rows != parsed.items or parsed.total != len(rows):
        raise SyncError("cash_shifts_rows_mismatch")
    if parsed.received_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    return dict(
        id=key,
        source_id=source.id,
        resource="cash_shifts",
        observed_at=parsed.received_at,
        sha256=parsed.sha256,
        raw=raw,
        payload=parsed.model_dump(mode="json"),
    )


def point_of_sale_bindings(raw: bytes, known_departments: set[UUID]) -> dict:
    """Read only direct group → POS membership; conflicting UUIDs remain unassigned."""
    root = ElementTree.fromstring(raw)
    if root.tag != "groupDtoes" or any(g.tag != "groupDto" for g in root):
        raise SyncError("cash_shifts_groups_invalid")
    candidates = defaultdict(set)
    for group in root:
        group_id = UUID(group.findtext("id", ""))
        department = group.findtext("departmentId")
        department_id = UUID(department) if department else None
        for pos in group.findall("./pointOfSaleDtoes/pointOfSaleDto"):
            candidates[UUID(pos.findtext("id", ""))].add(
                (department_id, group_id, pos.findtext("name"))
            )
    result = {}
    for pos, entries in candidates.items():
        if len(entries) != 1:
            result[pos] = {"mapping_state": "ambiguous"}
            continue
        department, group, name = next(iter(entries))
        if department not in known_departments:
            result[pos] = {"mapping_state": "unknown_department"}
            continue
        result[pos] = dict(
            mapping_state="matched",
            department_id=department,
            group_id=group,
            point_of_sale_name=name,
        )
    return result


def publish_cash_shifts(db, snapshot, groups) -> dict:
    report = CashShiftsResponse.model_validate(snapshot["payload"], by_name=True)
    query = report.request
    if (
        query.open_date_from != query.open_date_to
        or query.department_id
        or query.group_id
        or query.status != "ANY"
        or query.revision_from != -1
        or report.total != len(report.items)
        or len({r.id for r in report.items}) != report.total
        or snapshot["resource"] != "cash_shifts"
        or groups["resource"] != "groups"
        or snapshot["source_id"] != groups["source_id"]
        or str(report.snapshot_id) != str(snapshot["id"])
        or report.received_at != snapshot["observed_at"]
        or report.received_at.tzinfo is None
    ):
        raise SyncError("cash_shifts_incomplete")
    known = {
        r[0]
        for r in db.execute(
            "SELECT id FROM chaika.corporate_nodes WHERE source_id=%s AND type='DEPARTMENT'",
            (snapshot["source_id"],),
        )
    }
    bindings = point_of_sale_bindings(groups["raw"], known)
    counts = Counter(shifts=report.total, matched=0, unmapped=0, open=0, closed=0, accepted=0)
    columns = list(CashShift.model_fields) + [
        "snapshot_id",
        "groups_snapshot_id",
        "department_id",
        "group_id",
        "point_of_sale_name",
        "mapping_state",
    ]
    records = []
    for row in report.items:
        binding = bindings.get(row.point_of_sale_id, {"mapping_state": "missing"})
        counts["matched" if binding["mapping_state"] == "matched" else "unmapped"] += 1
        counts["open" if row.close_date is None else "closed"] += 1
        counts["accepted"] += row.accept_date is not None
        values = {
            **row.model_dump(),
            **binding,
            "snapshot_id": snapshot["id"],
            "groups_snapshot_id": groups["id"],
        }
        records.append(tuple(values.get(c) for c in columns))
    with db.transaction():
        with db.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    "INSERT INTO chaika.cash_shift_observations ({}) "
                    "VALUES ({}) ON CONFLICT(snapshot_id,id) DO NOTHING"
                ).format(
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.SQL(",").join(sql.Placeholder() for _ in columns),
                ),
                records,
            )
        db.execute(
            "INSERT INTO chaika.cash_shift_days"
            "(source_id,open_day,last_snapshot_id,observed_at,row_count) VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(source_id,open_day) DO UPDATE SET "
            "last_snapshot_id=EXCLUDED.last_snapshot_id,"
            "observed_at=EXCLUDED.observed_at,row_count=EXCLUDED.row_count "
            "WHERE EXCLUDED.observed_at>chaika.cash_shift_days.observed_at",
            (
                snapshot["source_id"],
                query.open_date_from,
                snapshot["id"],
                snapshot["observed_at"],
                report.total,
            ),
        )
    return dict(counts)


def synchronize_cash_shifts(
    settings: Settings,
    query: CashShiftSyncQuery,
    api_url="http://127.0.0.1:8010",
    *,
    on_day_saved: Callable[[dict], None] | None = None,
    stop: Event | None = None,
) -> dict:
    api_url = check_local_api(api_url)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    source = configured_sources(settings)[0]
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-cash-shifts-sync",
        ) as db,
        reference_lock(db),
    ):
        register_sources(db, [source])
        if not db.execute(
            "SELECT 1 FROM chaika.sources WHERE id=%s AND server_type='CHAIN'", (source.id,)
        ).fetchone():
            raise SyncError("reference_sync_required")
        db.execute(
            "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),"
            "error_code='interrupted' WHERE job='cash_shifts' AND status='running'"
        )
        run_id = uuid4()
        counts = dict(
            completed_days=0,
            total_days=(query.open_date_to - query.open_date_from).days + 1,
            shifts=0,
            matched=0,
            unmapped=0,
            date_from=query.open_date_from.isoformat(),
            date_to=query.open_date_to.isoformat(),
        )
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status,counts) "
            "VALUES(%s,'cash_shifts','running',%s)",
            (run_id, Jsonb(counts)),
        )
        days, failure, touched, logout_ok = [], None, False, True
        with httpx.Client(base_url=api_url, timeout=180, trust_env=False) as client:

            def get(path, **kwargs):
                r = client.get("/api/v1/iiko" + path, **kwargs)
                if r.status_code != 200:
                    raise SyncError(f"cash_shifts_http_{r.status_code}")
                return r.json()

            try:
                actual = [
                    r for r in get("/connections")["items"] if r["connection_id"] == source.id
                ]
                if len(actual) != 1 or actual[0]["base_url"] != source.base_url:
                    raise SyncError("backend_source_config_mismatch")
                touched = True
                groups = capture_snapshot(
                    BACKEND_DIR / ".local",
                    source,
                    "groups",
                    get("/connections/primary/corporate-groups"),
                )
                with db.transaction():
                    append_snapshot(db, run_id, groups)
                for offset in range(counts["total_days"]):
                    if stop is not None and stop.is_set():
                        raise SyncError("cash_shifts_interrupted")
                    day = query.open_date_from + timedelta(days=offset)
                    request = CashShiftsQuery(open_date_from=day, open_date_to=day)
                    payload = get(
                        "/cash-shifts",
                        params={"open_date_from": day.isoformat(), "open_date_to": day.isoformat()},
                    )
                    snapshot = capture_cash_shifts(settings, source, request, payload)
                    with db.transaction():
                        append_snapshot(db, run_id, snapshot)
                        result = publish_cash_shifts(db, snapshot, groups)
                        next_counts = {**counts, "completed_days": counts["completed_days"] + 1}
                        for key in ("shifts", "matched", "unmapped"):
                            next_counts[key] += result[key]
                        next_counts["completed_through"] = day.isoformat()
                        db.execute(
                            "UPDATE chaika.sync_runs SET counts=%s WHERE id=%s",
                            (Jsonb(next_counts), run_id),
                        )
                    counts = next_counts
                    saved_day = dict(day=day.isoformat(), snapshot_id=snapshot["id"], **result)
                    days.append(saved_day)
                    if on_day_saved is not None:
                        on_day_saved(saved_day)
            except BaseException as error:
                failure = error
            finally:
                if touched:
                    try:
                        r = client.post("/api/v1/iiko/connections/primary/logout")
                        logout_ok = r.status_code == 200 and r.json()["state"] == "logged_out"
                    except Exception:
                        logout_ok = False
        counts["logout_ok"] = logout_ok
        if failure is None and not logout_ok:
            failure = SyncError("cash_shifts_logout_failed")
        code = (
            (str(failure) if isinstance(failure, SyncError) else type(failure).__name__)
            if failure
            else None
        )
        db.execute(
            "UPDATE chaika.sync_runs SET status=%s,finished_at=now(),error_code=%s,"
            "counts=%s WHERE id=%s",
            ("failed" if failure else "succeeded", code, Jsonb(counts), run_id),
        )
        if failure:
            raise SyncError(code) from None
        return dict(
            run_id=str(run_id),
            status="succeeded",
            source_id=source.id,
            finished_at=datetime.now(UTC).isoformat(),
            days=days,
            counts=counts,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    args = parser.parse_args()
    try:
        result = synchronize_cash_shifts(
            Settings(), CashShiftSyncQuery(open_date_from=args.date_from, open_date_to=args.date_to)
        )
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
