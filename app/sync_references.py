"""One-shot reference synchronization via the running local FastAPI application.

Run: python -m app.sync_references. Credentials are read from backend/.env.
The database session lock covers collection, publication and iiko logout.
"""

import argparse
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.core.iiko_connections import read_connections
from app.schemas.iiko_topology import CorporateGroupsResponse, CorporateHierarchyResponse
from app.services.iiko_connections import read_server_type
from app.services.iiko_stores import read_stores
from app.services.iiko_topology import (
    map_departments,
    read_corporate_groups,
    read_departments,
    read_replication,
)

LOCK_ID = 7623011091001


class SyncError(Exception):
    """Only fixed, non-secret error codes should be passed to this exception."""


@contextmanager
def reference_lock(db: psycopg.Connection):
    if not db.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]:
        raise SyncError("sync_already_running")
    try:
        yield
    finally:
        # Release explicitly: the pooler can keep the underlying PG session alive.
        db.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    base_url: str
    fingerprint: str


def configured_sources(settings: Settings) -> list[Source]:
    definitions = [("primary", "Chain", settings)] + [
        (definition.id, definition.label, config)
        for definition, config in read_connections(settings)
    ]
    result = []
    for key, label, config in definitions:
        if not config.iiko_base_url or not config.iiko_login:
            raise SyncError("source_not_configured")
        base_url = str(config.iiko_base_url).rstrip("/")
        fingerprint = hashlib.sha256(f"{base_url}\n{config.iiko_login}".encode()).hexdigest()
        result.append(Source(key, label, base_url, fingerprint))
    return result


def check_local_api(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SyncError("api_must_be_loopback")
    return url.rstrip("/")


def capture_snapshot(directory: Path, source: Source, resource: str, response: dict) -> dict:
    """Validate immutable RAW against HTTP metadata and the configured source."""
    snapshot_id = str(UUID(response["snapshot_id"]))
    if resource == "stores":
        folder, suffix = directory / "stores", "xml"
        metadata = json.loads((folder / "current.json").read_text())
        if metadata["snapshot"] != response:
            raise SyncError("stores_snapshot_changed")
    else:
        folder = directory / ("connections" if resource == "server_type" else "topology")
        folder = folder / source.id
        suffix = "txt" if resource == "server_type" else "xml"
        metadata = json.loads((folder / f"{snapshot_id}.meta.json").read_text())
    if metadata["source_fingerprint"] != source.fingerprint:
        raise SyncError("raw_source_mismatch")
    raw_path = folder / f"{snapshot_id}.{suffix}"
    if raw_path.stat().st_size > 16 * 1024 * 1024:
        raise SyncError("raw_size_limit")
    raw = raw_path.read_bytes()
    if (
        len(raw) != response["source_bytes"]
        or hashlib.sha256(raw).hexdigest() != response["sha256"]
    ):
        raise SyncError("raw_hash_mismatch")
    if resource == "server_type":
        if read_server_type(raw_path) != response["server_type"]:
            raise SyncError("server_type_mismatch")
        payload = response
    else:
        if resource == "stores":
            rows = read_stores(raw_path, 16 * 1024 * 1024)
        else:
            rows = {
                "departments": read_departments,
                "groups": read_corporate_groups,
                "replication": read_replication,
            }[resource](raw_path)
        items = [row.model_dump(mode="json") for row in rows]
        if len(items) != response["total"] or ("items" in response and response["items"] != items):
            raise SyncError("raw_items_mismatch")
        payload = {**response, "items": items}
    observed_at = datetime.fromisoformat(response.get("received_at", response.get("checked_at")))
    if observed_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    return {
        "id": snapshot_id,
        "source_id": source.id,
        "resource": resource,
        "observed_at": observed_at,
        "sha256": response["sha256"],
        "raw": raw,
        "payload": payload,
    }


def register_sources(db: psycopg.Connection, sources: list[Source]) -> None:
    with db.transaction():
        for source in sources:
            existing = db.execute(
                "SELECT base_url, fingerprint FROM chaika.sources WHERE id = %s", (source.id,)
            ).fetchone()
            if existing and existing != (source.base_url, source.fingerprint):
                raise SyncError("source_identity_changed")
            db.execute(
                "INSERT INTO chaika.sources (id,label,base_url,fingerprint) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label",
                (source.id, source.label, source.base_url, source.fingerprint),
            )


def append_snapshot(db: psycopg.Connection, run_id: UUID, snapshot: dict) -> None:
    db.execute(
        "INSERT INTO chaika.raw_snapshots "
        "(id,run_id,source_id,resource,observed_at,sha256,source_bytes,raw,normalized) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            snapshot["id"],
            run_id,
            snapshot["source_id"],
            snapshot["resource"],
            snapshot["observed_at"],
            snapshot["sha256"],
            len(snapshot["raw"]),
            snapshot["raw"],
            Jsonb(snapshot["payload"]),
        ),
    )


def publish(db: psycopg.Connection, sources: list[Source], snapshots: dict) -> dict:
    """Atomically publish a complete collection; missing rows are never called deleted."""
    primary = snapshots[("primary", "departments")]
    stores = snapshots[("primary", "stores")]
    if not primary["payload"]["items"] or not stores["payload"]["items"]:
        raise SyncError("empty_primary_reference_set")
    hierarchies = {
        source.id: CorporateHierarchyResponse.model_validate(
            snapshots[(source.id, "departments")]["payload"], by_name=True
        )
        for source in sources
    }
    rms_ids = [s.id for s in sources if s.id != "primary"]
    groups = {
        key: CorporateGroupsResponse.model_validate(
            snapshots[(key, "groups")]["payload"], by_name=True
        )
        for key in rms_ids
    }
    mapping = map_departments(hierarchies["primary"], rms_ids, hierarchies, groups)
    counts = {
        "sources": len(sources),
        "corporate_nodes": len(primary["payload"]["items"]),
        "stores": len(stores["payload"]["items"]),
        "rms_bindings": len(mapping.bindings),
        "matched_rms": sum(b.state == "matched" for b in mapping.bindings),
        "unmapped_chain_departments": len(mapping.unmapped_chain_departments),
        "raw_snapshots": len(snapshots),
        "replication_status_counts": snapshots[("primary", "replication")]["payload"][
            "status_counts"
        ],
    }
    with db.transaction():
        db.execute("UPDATE chaika.sources SET configured = false")
        for source in sources:
            observed = snapshots[(source.id, "server_type")]
            db.execute(
                "UPDATE chaika.sources SET configured=true,server_type=%s,verified_at=%s "
                "WHERE id=%s",
                (observed["payload"]["server_type"], observed["observed_at"], source.id),
            )
        for table, snapshot in [("corporate_nodes", primary), ("stores", stores)]:
            db.execute(
                sql.SQL("UPDATE chaika.{} SET present_in_latest=false WHERE source_id=%s").format(
                    sql.Identifier(table)
                ),
                ("primary",),
            )
            columns = ["source_id", "id", "parent_id", "code", "name"]
            if table == "corporate_nodes":
                columns.append("type")
            columns += ["first_seen_at", "last_seen_at", "last_snapshot_id"]
            updates = [c for c in columns if c not in {"source_id", "id", "first_seen_at"}]
            statement = sql.SQL(
                "INSERT INTO chaika.{} ({}) VALUES ({}) ON CONFLICT (source_id,id) "
                "DO UPDATE SET {}, present_in_latest=true"
            ).format(
                sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns),
                sql.SQL(",").join(
                    sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                    for c in updates
                ),
            )
            for row in snapshot["payload"]["items"]:
                values = {
                    **row,
                    "source_id": "primary",
                    "first_seen_at": snapshot["observed_at"],
                    "last_seen_at": snapshot["observed_at"],
                    "last_snapshot_id": snapshot["id"],
                }
                db.execute(statement, [values.get(c) for c in columns])
        # Clear old matches inside this transaction, allowing restaurants to swap RMS.
        db.execute("UPDATE chaika.rms_bindings SET state='not_configured',department_id=NULL")
        for binding in mapping.bindings:
            db.execute(
                "INSERT INTO chaika.rms_bindings (source_id,chain_source_id,department_id,state,"
                "details,observed_at,chain_snapshot_id,rms_snapshot_id,groups_snapshot_id) "
                "VALUES (%s,'primary',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (source_id) DO UPDATE SET "
                "department_id=EXCLUDED.department_id,state=EXCLUDED.state,details=EXCLUDED.details,"
                "observed_at=EXCLUDED.observed_at,chain_snapshot_id=EXCLUDED.chain_snapshot_id,"
                "rms_snapshot_id=EXCLUDED.rms_snapshot_id,groups_snapshot_id=EXCLUDED.groups_snapshot_id",
                (
                    binding.connection_id,
                    binding.department_id if binding.state == "matched" else None,
                    binding.state,
                    Jsonb(binding.model_dump(mode="json")),
                    datetime.now(UTC),
                    primary["id"],
                    binding.rms_snapshot_id,
                    binding.groups_snapshot_id,
                ),
            )
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return counts


def synchronize(settings: Settings, api_url: str, output: Path) -> dict:
    api_url = check_local_api(api_url)
    sources = configured_sources(settings)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    run_id = uuid4()
    # Autocommit preserves RAW and the run journal if later collection/publication fails.
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-reference-sync",
        ) as db,
        reference_lock(db),
    ):
        register_sources(db, sources)
        db.execute(
            "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),"
            "error_code='interrupted' WHERE status='running'"
        )
        db.execute("INSERT INTO chaika.sync_runs (id,status) VALUES (%s,'running')", (run_id,))
        snapshots, touched, logout_errors = {}, [], []
        counts, failure = {}, None
        with httpx.Client(
            base_url=api_url, timeout=180, trust_env=False, follow_redirects=False
        ) as client:

            def request(method: str, path: str) -> dict:
                response = client.request(method, "/api/v1/iiko" + path)
                if response.status_code != 200:
                    raise SyncError(f"backend_http_{response.status_code}")
                return response.json()

            def load(source: Source, resource: str, method: str, path: str) -> None:
                if source.id not in touched:
                    touched.append(source.id)
                payload = request(method, path)
                snapshot = capture_snapshot(BACKEND_DIR / ".local", source, resource, payload)
                append_snapshot(db, run_id, snapshot)
                snapshots[(source.id, resource)] = snapshot
                print(json.dumps({"source": source.id, "loaded": resource}), flush=True)

            try:
                actual = request("GET", "/connections")["items"]
                expected = {s.id: s.base_url for s in sources}
                if {r["connection_id"]: r["base_url"] for r in actual} != expected:
                    raise SyncError("backend_source_config_mismatch")
                for source in sources:
                    load(source, "server_type", "GET", f"/connections/{source.id}/server-type")
                    server_type = snapshots[(source.id, "server_type")]["payload"]["server_type"]
                    if server_type != ("CHAIN" if source.id == "primary" else "REPLICATED_RMS"):
                        raise SyncError("unexpected_server_type")
                    load(source, "departments", "GET", f"/connections/{source.id}/departments")
                    if source.id != "primary":
                        load(source, "groups", "GET", f"/connections/{source.id}/corporate-groups")
                load(sources[0], "replication", "GET", "/replication/statuses")
                load(sources[0], "stores", "POST", "/stores/load")
            except BaseException as error:
                failure = error
            finally:
                for source_id in touched:
                    try:
                        result = request("POST", f"/connections/{source_id}/logout")
                        if result["state"] != "logged_out":
                            raise SyncError("logout_not_confirmed")
                    except Exception:
                        logout_errors.append(source_id)
            if logout_errors and failure is None:
                failure = SyncError("logout_failed")
            if failure is None:
                try:
                    # Publication and successful run status commit together.
                    with db.transaction():
                        counts = publish(db, sources, snapshots)
                        db.execute(
                            "UPDATE chaika.sync_runs SET status='succeeded',finished_at=now(),"
                            "counts=%s WHERE id=%s",
                            (Jsonb(counts), run_id),
                        )
                except BaseException as error:
                    failure = error
        if failure is not None:
            code = str(failure) if isinstance(failure, SyncError) else type(failure).__name__
            db.execute(
                "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),error_code=%s,"
                "counts=%s WHERE id=%s",
                (
                    code,
                    Jsonb({"raw_snapshots": len(snapshots), "logout_errors": logout_errors}),
                    run_id,
                ),
            )
        else:
            code = None
        report = {
            "run_id": str(run_id),
            "status": "failed" if failure else "succeeded",
            "counts": counts,
            "error_code": code,
            "logout_errors": logout_errors,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if failure:
            raise SyncError(code) from None
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument("--output", type=Path, default=BACKEND_DIR / ".local/sync/latest.json")
    args = parser.parse_args()
    try:
        report = synchronize(Settings(), args.api_url, args.output)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
