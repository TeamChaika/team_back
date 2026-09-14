"""One-shot products, invoices, writeoffs and assembly charts synchronization."""

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.invoice_lines import numbered_invoice_items
from app.schemas.iiko_outgoing import OutgoingInvoicesQuery
from app.schemas.iiko_transfers import TransfersQuery
from app.schemas.iiko_writeoffs import WriteoffsQuery
from app.services.iiko_assembly import read_all_assembly
from app.services.iiko_groups import read_groups
from app.services.iiko_invoices import read_incoming_invoices
from app.services.iiko_outgoing import read_outgoing_invoices
from app.services.iiko_products import read_products
from app.services.iiko_transfers import read_transfers
from app.services.iiko_writeoffs import read_writeoffs
from app.sync_references import (
    Source,
    SyncError,
    append_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)

RESOURCES = ("product_groups", "products", "incoming_invoices", "writeoffs", "assembly_charts")


def capture_inventory(
    settings: Settings, source: Source, resource: str, response: dict, day: date
) -> dict:
    key = str(UUID(response["snapshot_id"]))
    folder_name = {
        "product_groups": "groups",
        "incoming_invoices": "incoming-invoices",
        "outgoing_invoices": "outgoing-invoices",
        "assembly_charts": "assembly-charts",
    }.get(resource, resource)
    folder = BACKEND_DIR / ".local" / folder_name
    catalog = resource in {"products", "product_groups"}
    meta = json.loads((folder / ("current.json" if catalog else f"{key}.meta.json")).read_text())
    if meta["source_fingerprint"] != source.fingerprint:
        raise SyncError("inventory_source_mismatch")
    if catalog and meta["snapshot"] != response:
        raise SyncError("inventory_snapshot_changed")
    maximum = {
        "products": settings.iiko_products_max_response_bytes,
        "product_groups": settings.iiko_groups_max_response_bytes,
        "incoming_invoices": settings.iiko_invoices_max_response_bytes,
        "outgoing_invoices": settings.iiko_outgoing_max_response_bytes,
        "writeoffs": settings.iiko_writeoffs_max_response_bytes,
        "transfers": settings.iiko_transfers_max_response_bytes,
        "assembly_charts": settings.iiko_all_assembly_max_response_bytes,
    }[resource]
    suffix = "xml" if resource in {"incoming_invoices", "outgoing_invoices"} else "json"
    path = folder / f"{key}.{suffix}"
    if path.stat().st_size > maximum:
        raise SyncError("inventory_raw_too_large")
    raw = path.read_bytes()
    if (
        len(raw) != response["source_bytes"]
        or hashlib.sha256(raw).hexdigest() != response["sha256"]
    ):
        raise SyncError("inventory_raw_mismatch")
    if resource == "products":
        rows = read_products(path, maximum)
    elif resource == "product_groups":
        rows = read_groups(path, maximum)
    elif resource == "incoming_invoices":
        rows = read_incoming_invoices(path)
    elif resource == "outgoing_invoices":
        expected = OutgoingInvoicesQuery(date_from=day, date_to=day)
        if (
            meta.get("source_endpoint") != "documents/export/outgoingInvoice"
            or {k: v for k, v in meta.items() if k not in {"source_endpoint", "source_fingerprint"}}
            != {k: v for k, v in response.items() if k != "documents"}
            or response["request"] != expected.model_dump(mode="json")
        ):
            raise SyncError("outgoing_snapshot_scope_mismatch")
        rows = read_outgoing_invoices(path, expected)
    elif resource == "transfers":
        expected = TransfersQuery(date_from=day, date_to=day)
        if (
            meta.get("source_endpoint") != "v2/documents/internalTransfer"
            or {k: v for k, v in meta.items() if k not in {"source_endpoint", "source_fingerprint"}}
            != {k: v for k, v in response.items() if k != "documents"}
            or response["request"] != expected.model_dump(mode="json")
        ):
            raise SyncError("transfer_snapshot_scope_mismatch")
        export = read_transfers(path, expected)
        if export.revision != response["revision"]:
            raise SyncError("transfer_revision_mismatch")
        rows = export.response
    elif resource == "writeoffs":
        export = read_writeoffs(path, WriteoffsQuery(date_from=day, date_to=day))
        if export.revision != response["revision"]:
            raise SyncError("writeoff_revision_mismatch")
        rows = export.response
    else:
        export = read_all_assembly(path, day, maximum)
        if export.known_revision != response["known_revision"]:
            raise SyncError("chart_revision_mismatch")
        rows = export.assembly_charts
    items = [r.model_dump(mode="json") for r in rows]
    if len(items) != response["total"]:
        raise SyncError("inventory_count_mismatch")
    if "documents" in response and response["documents"] != items:
        raise SyncError("inventory_document_mismatch")
    if "items_count" in response and sum(len(r["items"]) for r in items) != response["items_count"]:
        raise SyncError("inventory_line_count_mismatch")
    if not catalog:
        if resource == "assembly_charts":
            valid_scope = (
                response["business_date"] == day.isoformat()
                and response["date_to_exclusive"] == (day + timedelta(days=1)).isoformat()
                and response["include_deleted_products"] is True
                and response["include_prepared_charts"] is False
            )
        else:
            request = response["request"]
            valid_scope = (
                request["date_from"] == request["date_to"] == day.isoformat()
                and (resource == "outgoing_invoices" or request["revision_from"] == -1)
                and not request.get("supplier_id")
                and request.get("status") is None
            )
        if not valid_scope:
            raise SyncError("inventory_scope_mismatch")
    observed_at = datetime.fromisoformat(response["received_at"])
    if observed_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    payload = {k: v for k, v in response.items() if k != "documents"}
    payload.update(
        items=items, sync_business_date=day.isoformat(), source_fingerprint=source.fingerprint
    )
    return {
        "id": key,
        "source_id": source.id,
        "resource": resource,
        "observed_at": observed_at,
        "sha256": response["sha256"],
        "raw": raw,
        "payload": payload,
    }


def upsert_rows(db, table: str, columns: list[str], keys: list[str], rows) -> int:
    """Pipeline bounded batches, preserving exact decimal strings for numeric columns."""
    updates = [c for c in columns if c not in {*keys, "first_seen_at"}]
    statement = sql.SQL(
        "INSERT INTO chaika.{} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}"
    ).format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
        sql.SQL(",").join(map(sql.Identifier, keys)),
        sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in updates
        ),
    )
    iterator, count = iter(rows), 0
    with db.cursor() as cursor:
        while batch := list(islice(iterator, 1000)):
            cursor.executemany(statement, [[r.get(c) for c in columns] for r in batch])
            count += len(batch)
    return count


def publish_writeoffs(db, snapshot: dict, day: date) -> None:
    """Publish one writeoff day inside the caller's RAW/checkpoint transaction."""
    parents = snapshot["payload"]["items"]
    source_id = snapshot["source_id"]

    def headers():
        for row in parents:
            details = {k: v for k, v in row.items() if k != "items"}
            yield {
                **details,
                "source_id": source_id,
                "last_export_date": day,
                "first_seen_at": snapshot["observed_at"],
                "last_seen_at": snapshot["observed_at"],
                "last_snapshot_id": snapshot["id"],
                "details": Jsonb(details),
            }

    upsert_rows(
        db,
        "writeoffs",
        [
            "source_id",
            "id",
            "document_number",
            "date_incoming",
            "status",
            "store_id",
            "account_id",
            "last_export_date",
            "first_seen_at",
            "last_seen_at",
            "last_snapshot_id",
            "details",
        ],
        ["source_id", "id"],
        headers(),
    )
    db.execute(
        "UPDATE chaika.writeoff_items SET present_in_latest=false "
        "WHERE source_id=%s AND document_id=ANY(%s::uuid[])",
        (source_id, [p["id"] for p in parents]),
    )
    upsert_rows(
        db,
        "writeoff_items",
        [
            "source_id",
            "document_id",
            "num",
            "product_id",
            "amount",
            "cost",
            "measure_unit_id",
            "amount_factor",
            "details",
            "present_in_latest",
        ],
        ["source_id", "document_id", "num"],
        (
            {
                **item,
                "source_id": source_id,
                "document_id": parent["id"],
                "details": Jsonb(item),
                "present_in_latest": True,
            }
            for parent in parents
            for item in parent["items"]
        ),
    )


def publish_inventory(db, snapshots: dict, day: date) -> dict:
    if set(snapshots) != set(RESOURCES):
        raise SyncError("inventory_incomplete")
    if not snapshots["products"]["payload"]["items"]:
        raise SyncError("empty_product_catalog")
    counts = {}
    meta_columns = ["source_id", "first_seen_at", "last_seen_at", "last_snapshot_id", "details"]
    with db.transaction():
        for resource, table, fields in [
            (
                "product_groups",
                "product_groups",
                ["id", "name", "parent_id", "code", "num", "deleted"],
            ),
            (
                "products",
                "products",
                [
                    "id",
                    "name",
                    "type",
                    "group_id",
                    "main_unit_id",
                    "category_id",
                    "code",
                    "num",
                    "deleted",
                    "default_sale_price",
                    "unit_weight",
                    "unit_capacity",
                ],
            ),
            (
                "incoming_invoices",
                "incoming_invoices",
                [
                    "id",
                    "document_number",
                    "date_incoming",
                    "incoming_date",
                    "status",
                    "supplier_id",
                    "default_store_id",
                    "revision",
                    "last_export_date",
                ],
            ),
            (
                "writeoffs",
                "writeoffs",
                [
                    "id",
                    "document_number",
                    "date_incoming",
                    "status",
                    "store_id",
                    "account_id",
                    "last_export_date",
                ],
            ),
            (
                "assembly_charts",
                "assembly_charts",
                ["id", "product_id", "date_from", "date_to", "assembled_amount"],
            ),
        ]:
            snapshot = snapshots[resource]
            catalog = resource in {"product_groups", "products"}
            if catalog:
                db.execute(
                    sql.SQL(
                        "UPDATE chaika.{} SET present_in_latest=false WHERE source_id='primary'"
                    ).format(sql.Identifier(table))
                )
            columns = fields + meta_columns + (["present_in_latest"] if catalog else [])

            def header_rows(snapshot=snapshot):
                for row in snapshot["payload"]["items"]:
                    details = {k: v for k, v in row.items() if k != "items"}
                    yield {
                        **details,
                        "source_id": "primary",
                        "group_id": row.get("parent_id"),
                        "product_id": row.get("assembled_product_id"),
                        "last_export_date": day,
                        "first_seen_at": snapshot["observed_at"],
                        "last_seen_at": snapshot["observed_at"],
                        "last_snapshot_id": snapshot["id"],
                        "details": Jsonb(details),
                        "present_in_latest": True,
                    }

            counts[resource] = upsert_rows(db, table, columns, ["source_id", "id"], header_rows())
        for resource, table, parent_column, fields, item_key in [
            (
                "incoming_invoices",
                "incoming_invoice_items",
                "document_id",
                [
                    "num",
                    "num_occurrence",
                    "product_id",
                    "store_id",
                    "amount",
                    "actual_amount",
                    "price",
                    "sum",
                    "amount_unit_id",
                ],
                "num",
            ),
            (
                "writeoffs",
                "writeoff_items",
                "document_id",
                ["num", "product_id", "amount", "cost", "measure_unit_id", "amount_factor"],
                "num",
            ),
            (
                "assembly_charts",
                "assembly_chart_items",
                "chart_id",
                ["id", "product_id", "amount_in", "amount_middle", "amount_out", "sort_weight"],
                "id",
            ),
        ]:
            parents = snapshots[resource]["payload"]["items"]
            ids = [r["id"] for r in parents]
            db.execute(
                sql.SQL(
                    "UPDATE chaika.{} SET present_in_latest=false "
                    "WHERE source_id='primary' AND {}=ANY(%s::uuid[])"
                ).format(sql.Identifier(table), sql.Identifier(parent_column)),
                (ids,),
            )

            def item_rows(parents=parents, parent_column=parent_column, resource=resource):
                for parent in parents:
                    numbered = (
                        numbered_invoice_items(parent["items"])
                        if resource == "incoming_invoices"
                        else ((item, 1) for item in parent["items"])
                    )
                    for item, occurrence in numbered:
                        yield {
                            **item,
                            "source_id": "primary",
                            parent_column: parent["id"],
                            "num_occurrence": occurrence,
                            "details": Jsonb(item),
                            "present_in_latest": True,
                        }

            counts[table] = upsert_rows(
                db,
                table,
                ["source_id", parent_column] + fields + ["details", "present_in_latest"],
                ["source_id", parent_column, item_key]
                + (["num_occurrence"] if resource == "incoming_invoices" else []),
                item_rows(),
            )
        chart_snapshot = snapshots["assembly_charts"]
        db.execute(
            "UPDATE chaika.assembly_chart_scopes SET present_in_latest=false "
            "WHERE source_id='primary' AND business_date=%s",
            (day,),
        )
        upsert_rows(
            db,
            "assembly_chart_scopes",
            ["source_id", "business_date", "chart_id", "last_snapshot_id", "present_in_latest"],
            ["source_id", "business_date", "chart_id"],
            (
                {
                    "source_id": "primary",
                    "business_date": day,
                    "chart_id": c["id"],
                    "last_snapshot_id": chart_snapshot["id"],
                    "present_in_latest": True,
                }
                for c in chart_snapshot["payload"]["items"]
            ),
        )
    return counts


def restore_snapshots(db, run_id: UUID, source: Source, day: date) -> dict:
    job = db.execute(
        "SELECT job,status,counts FROM chaika.sync_runs WHERE id=%s", (run_id,)
    ).fetchone()
    if not job or job[0] != "inventory" or job[1] == "running":
        raise SyncError("inventory_resume_run_invalid")
    snapshots = {}
    manifest = job[2].get("snapshot_ids", {})
    ids = [str(UUID(value)) for value in manifest.values()]
    for key, resource, observed_at, payload, valid in db.execute(
        "SELECT id,resource,observed_at,normalized,encode(sha256(raw),'hex')=sha256 "
        "FROM chaika.raw_snapshots WHERE (run_id=%s OR id=ANY(%s::uuid[])) "
        "AND source_id=%s ORDER BY observed_at",
        (run_id, ids, source.id),
    ):
        if (
            resource not in RESOURCES
            or not valid
            or payload.get("source_fingerprint") != source.fingerprint
            or payload.get("sync_business_date") != day.isoformat()
        ):
            raise SyncError("inventory_resume_scope_mismatch")
        snapshots[resource] = {"id": str(key), "observed_at": observed_at, "payload": payload}
    return snapshots


def synchronize_inventory(
    settings: Settings, api_url: str, day: date, output: Path, resume_run: UUID | None = None
) -> dict:
    api_url = check_local_api(api_url)
    source = configured_sources(settings)[0]
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    run_id = uuid4()
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-inventory-sync",
        ) as db,
        reference_lock(db),
    ):
        register_sources(db, [source])
        if not db.execute(
            "SELECT 1 FROM chaika.sources WHERE id='primary' AND server_type='CHAIN'"
        ).fetchone():
            raise SyncError("reference_sync_required")
        db.execute(
            "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),"
            "error_code='interrupted' WHERE status='running'"
        )
        snapshots = restore_snapshots(db, resume_run, source, day) if resume_run else {}
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'inventory','running')",
            (run_id,),
        )
        touched, failure, logout_ok, counts = False, None, True, {}
        with httpx.Client(base_url=api_url, timeout=300, trust_env=False) as client:
            try:
                primary = client.get("/api/v1/iiko/connections")
                primary.raise_for_status()
                matched = [r for r in primary.json()["items"] if r["connection_id"] == "primary"]
                if len(matched) != 1 or matched[0]["base_url"] != source.base_url:
                    raise SyncError("backend_source_config_mismatch")
                requests = [
                    ("product_groups", "POST", "/groups/load", None),
                    ("products", "POST", "/products/load", None),
                    (
                        "incoming_invoices",
                        "GET",
                        "/incoming-invoices",
                        {"date_from": day.isoformat(), "date_to": day.isoformat()},
                    ),
                    (
                        "writeoffs",
                        "GET",
                        "/writeoffs",
                        {"date_from": day.isoformat(), "date_to": day.isoformat()},
                    ),
                    ("assembly_charts", "GET", "/assembly-charts/all", {"date": day.isoformat()}),
                ]
                for resource, method, path, params in requests:
                    if resource in snapshots:
                        continue
                    touched = True
                    response = client.request(method, "/api/v1/iiko" + path, params=params)
                    if response.status_code != 200:
                        raise SyncError(f"inventory_{resource}_http_{response.status_code}")
                    snapshot = capture_inventory(settings, source, resource, response.json(), day)
                    append_snapshot(db, run_id, snapshot)
                    snapshots[resource] = {k: v for k, v in snapshot.items() if k != "raw"}
                    del snapshot
                    print(
                        json.dumps(
                            {"loaded": resource, "total": snapshots[resource]["payload"]["total"]}
                        ),
                        flush=True,
                    )
            except BaseException as error:
                failure = error
            finally:
                if touched:
                    try:
                        response = client.post("/api/v1/iiko/connections/primary/logout")
                        logout_ok = (
                            response.status_code == 200 and response.json()["state"] == "logged_out"
                        )
                    except Exception:
                        logout_ok = False
            if not logout_ok and failure is None:
                failure = SyncError("inventory_logout_failed")
            if failure is None:
                try:
                    with db.transaction():
                        counts = publish_inventory(db, snapshots, day)
                        counts.update(
                            business_date=day.isoformat(),
                            snapshot_ids={k: v["id"] for k, v in snapshots.items()},
                            resumed_from=str(resume_run) if resume_run else None,
                        )
                        db.execute(
                            "UPDATE chaika.sync_runs SET status='succeeded',finished_at=now(),"
                            "counts=%s WHERE id=%s",
                            (Jsonb(counts), run_id),
                        )
                except BaseException as error:
                    failure = error
        code = (
            (str(failure) if isinstance(failure, SyncError) else type(failure).__name__)
            if failure
            else None
        )
        if failure:
            db.execute(
                "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),"
                "error_code=%s,counts=%s WHERE id=%s",
                (
                    code,
                    Jsonb(
                        {
                            "snapshot_ids": {k: v["id"] for k, v in snapshots.items()},
                            "business_date": day.isoformat(),
                            "resumed_from": str(resume_run) if resume_run else None,
                        }
                    ),
                    run_id,
                ),
            )
        report = {
            "run_id": str(run_id),
            "status": "failed" if failure else "succeeded",
            "business_date": day.isoformat(),
            "counts": counts,
            "error_code": code,
            "logout_ok": logout_ok,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if failure:
            raise SyncError(code) from None
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo("Europe/Simferopol")).date() - timedelta(days=1),
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument(
        "--output", type=Path, default=BACKEND_DIR / ".local/sync/inventory-latest.json"
    )
    parser.add_argument(
        "--resume-run",
        type=UUID,
        help="Reuse already validated RAW for this day; fetch missing resources",
    )
    args = parser.parse_args()
    try:
        report = synchronize_inventory(
            Settings(), args.api_url, args.date, args.output, args.resume_run
        )
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
