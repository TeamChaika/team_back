"""Collect and atomically publish suppliers, measure units and product categories."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.dictionaries import BUNDLED_DICTIONARIES, DICTIONARIES, DictionaryKind
from app.schemas.iiko_dictionaries import DictionarySnapshot
from app.services.iiko_dictionaries import MODELS, read_dictionary, summarize_dictionary
from app.sync_inventory import upsert_rows
from app.sync_references import (
    Source,
    SyncError,
    append_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)


def capture_dictionary(
    settings: Settings, source: Source, kind: DictionaryKind, response: dict
) -> dict:
    folder = BACKEND_DIR / ".local/dictionaries" / kind
    spec = DICTIONARIES[kind]
    metadata = json.loads((folder / "current.json").read_text())
    if metadata["source_fingerprint"] != source.fingerprint:
        raise SyncError("dictionary_source_mismatch")
    if metadata["snapshot"] != response:
        raise SyncError("dictionary_snapshot_changed")
    info = DictionarySnapshot.model_validate(response)
    if info.kind != kind or info.source_endpoint != spec.endpoint or info.request != spec.params:
        raise SyncError("dictionary_scope_mismatch")
    path = folder / f"{info.snapshot_id}.{spec.format}"
    maximum = settings.iiko_dictionaries_max_response_bytes
    if path.stat().st_size > maximum:
        raise SyncError("dictionary_raw_too_large")
    raw = path.read_bytes()
    if len(raw) != info.source_bytes or hashlib.sha256(raw).hexdigest() != info.sha256:
        raise SyncError("dictionary_raw_mismatch")
    rows = read_dictionary(path, maximum, kind=kind)
    if len(rows) != info.total or any(
        response[k] != v for k, v in summarize_dictionary(rows, kind=kind).items()
    ):
        raise SyncError("dictionary_count_mismatch")
    if info.received_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    return {
        "id": str(info.snapshot_id),
        "source_id": source.id,
        "resource": spec.table,
        "observed_at": info.received_at,
        "sha256": info.sha256,
        "raw": raw,
        "payload": {
            **response,
            "source_fingerprint": source.fingerprint,
            "items": [r.model_dump(mode="json") for r in rows],
        },
    }


def reference_links(db, source_id: str) -> dict:
    """Count unresolved original UUIDs, including historical document references."""
    result = {}
    checks = [
        (
            "invoice_suppliers",
            "SELECT DISTINCT supplier_id AS id FROM chaika.incoming_invoices WHERE source_id=%s",
            "counteragents",
        ),
        (
            "product_units",
            "SELECT DISTINCT main_unit_id AS id FROM chaika.products WHERE source_id=%s",
            "measure_units",
        ),
        (
            "invoice_units",
            "SELECT DISTINCT amount_unit_id AS id FROM chaika.incoming_invoice_items "
            "WHERE source_id=%s AND present_in_latest",
            "measure_units",
        ),
        (
            "writeoff_units",
            "SELECT DISTINCT measure_unit_id AS id FROM chaika.writeoff_items "
            "WHERE source_id=%s AND present_in_latest",
            "measure_units",
        ),
        (
            "product_categories",
            "SELECT DISTINCT category_id AS id FROM chaika.products WHERE source_id=%s",
            "product_categories",
        ),
        (
            "group_categories",
            "SELECT DISTINCT (details->>'category_id')::uuid AS id FROM chaika.product_groups "
            "WHERE source_id=%s",
            "product_categories",
        ),
        (
            "counteragent_stores",
            "SELECT DISTINCT represented_store_id AS id FROM chaika.counteragents "
            "WHERE source_id=%s",
            "stores",
        ),
    ]
    for label, query, table in checks:
        total, missing = db.execute(
            sql.SQL(
                "WITH refs AS ({}) SELECT count(*),count(*) FILTER(WHERE d.id IS NULL) "
                "FROM refs r LEFT JOIN chaika.{} d ON d.source_id=%s AND d.id=r.id "
                "WHERE r.id IS NOT NULL"
            ).format(sql.SQL(query), sql.Identifier(table)),
            (source_id, source_id),
        ).fetchone()
        result[f"{label}_ids"] = total
        result[f"{label}_unmatched"] = missing
    return result


def publish_dictionaries(db, snapshots: dict) -> tuple[dict, dict]:
    if (
        set(snapshots) != set(BUNDLED_DICTIONARIES)
        or len({s["source_id"] for s in snapshots.values()}) != 1
    ):
        raise SyncError("dictionary_bundle_incomplete")
    counts = {}
    with db.transaction():
        for kind in BUNDLED_DICTIONARIES:
            spec = DICTIONARIES[kind]
            snap = snapshots[kind]
            info = DictionarySnapshot.model_validate(snap["payload"])
            rows = [MODELS[kind].model_validate(r, by_name=True) for r in snap["payload"]["items"]]
            if (
                info.kind != kind
                or info.request != spec.params
                or info.source_endpoint != spec.endpoint
                or info.total != len(rows)
                or len({r.id for r in rows}) != len(rows)
                or str(info.snapshot_id) != str(snap["id"])
                or snap["resource"] != spec.table
            ):
                raise SyncError("dictionary_scope_mismatch")
            db.execute(
                sql.SQL("UPDATE chaika.{} SET present_in_latest=false WHERE source_id=%s").format(
                    sql.Identifier(spec.table)
                ),
                (snap["source_id"],),
            )

            def records(rows=rows, snap=snap):
                for row in rows:
                    yield {
                        **row.model_dump(),
                        "source_id": snap["source_id"],
                        "present_in_latest": True,
                        "first_seen_at": snap["observed_at"],
                        "last_seen_at": snap["observed_at"],
                        "last_snapshot_id": snap["id"],
                        "details": Jsonb(row.model_dump(mode="json")),
                    }

            counts[spec.table] = upsert_rows(
                db,
                spec.table,
                list(MODELS[kind].model_fields)
                + [
                    "source_id",
                    "present_in_latest",
                    "first_seen_at",
                    "last_seen_at",
                    "last_snapshot_id",
                    "details",
                ],
                ["source_id", "id"],
                records(),
            )
            counts[f"{spec.table}_deleted"] = sum(r.deleted is True for r in rows)
        links = reference_links(db, snap["source_id"])
    return counts, links


def synchronize_dictionaries(settings: Settings, api_url: str = "http://127.0.0.1:8010") -> dict:
    api_url = check_local_api(api_url)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    source = configured_sources(settings)[0]
    run_id = uuid4()
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-dictionary-sync",
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
            "error_code='interrupted' "
            "WHERE job='dictionaries' AND status='running'"
        )
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'dictionaries','running')",
            (run_id,),
        )
        snapshots = {}
        touched, logout_ok, failure = False, True, None
        with httpx.Client(base_url=api_url, timeout=180, trust_env=False) as client:
            try:
                response = client.get("/api/v1/iiko/connections")
                response.raise_for_status()
                primary = [r for r in response.json()["items"] if r["connection_id"] == source.id]
                if len(primary) != 1 or primary[0]["base_url"] != source.base_url:
                    raise SyncError("backend_source_config_mismatch")
                for kind in BUNDLED_DICTIONARIES:
                    touched = True
                    response = client.post(f"/api/v1/iiko/dictionaries/{kind}/load")
                    if response.status_code != 200:
                        raise SyncError(f"dictionary_{kind}_http_{response.status_code}")
                    snapshots[kind] = capture_dictionary(settings, source, kind, response.json())
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
        if failure is None and not logout_ok:
            failure = SyncError("dictionary_logout_failed")
        if failure is None:
            try:
                with db.transaction():
                    for snap in snapshots.values():
                        append_snapshot(db, run_id, snap)
                    counts, links = publish_dictionaries(db, snapshots)
                    counts["logout_ok"] = True
                    db.execute(
                        "UPDATE chaika.sync_runs SET status='succeeded',finished_at=now(),"
                        "counts=%s WHERE id=%s",
                        (Jsonb({**counts, "links": links}), run_id),
                    )
            except BaseException as error:
                failure = error
        if failure is not None:
            code = str(failure) if isinstance(failure, SyncError) else type(failure).__name__
            db.execute(
                "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),error_code=%s,"
                "counts=%s WHERE id=%s",
                (code, Jsonb({"logout_ok": logout_ok}), run_id),
            )
            raise SyncError(code) from None
        return {
            "run_id": str(run_id),
            "status": "succeeded",
            "source_id": source.id,
            "counts": counts,
            "links": links,
            "snapshots": {k: s["id"] for k, s in snapshots.items()},
            "finished_at": datetime.now(UTC).isoformat(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    try:
        result = synchronize_dictionaries(Settings(), args.api_url)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
