"""A standalone restaurant retains one real source and evidence-backed isolation."""

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app import event_storage, source_capabilities, sync_event_history, sync_events, sync_references
from app.api.routes import iiko_events as event_routes
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.schemas.iiko_events import EventsCapture, EventsSyncQuery
from app.services.iiko_connections import read_server_type
from app.services.iiko_events import capture_events
from app.sync_references import Source, SyncError, reference_mode
from app.tenancy.migrations import MIGRATION_DIRECTORY, provision_tenant
from app.tenancy.sql import validate_database_runtime
from tests.test_iiko_connections import config
from tests.test_reference_sync import bundle as bundle
from tests.test_rms_events import DAY, HEADERS, META, pair, xml
from tests.test_tenant_migrations_postgres import empty_database as empty_database


@pytest.mark.parametrize(
    "payload",
    [
        b"STANDALONE_RMS",
        b'"STANDALONE_RMS"',
        b'<?xml version="1.0"?><serverType>STANDALONE_RMS</serverType>',
    ],
)
def test_server_type_preserves_real_standalone_enum(tmp_path, payload):
    path = tmp_path / "type"
    path.write_bytes(payload)
    assert read_server_type(path) == "STANDALONE_RMS"


@pytest.mark.parametrize(
    "payload",
    [
        b"<serverType>UNKNOWN</serverType>",
        b"<other>STANDALONE_RMS</other>",
        b"<serverType><value>STANDALONE_RMS</value></serverType>",
        b'<serverType mode="own">STANDALONE_RMS</serverType>',
        b'<!DOCTYPE serverType [<!ENTITY mode "STANDALONE_RMS">]><serverType>&mode;</serverType>',
        b"<serverType>",
        b"x" * 16385,
    ],
)
def test_server_type_rejects_ambiguous_unsafe_or_oversized_xml(tmp_path, payload):
    path = tmp_path / "type"
    path.write_bytes(payload)
    with pytest.raises(IikoError):
        read_server_type(path)


def test_reference_modes_reject_duplicate_or_wrong_server_topology():
    assert reference_mode({"primary": "CHAIN", "rms-a": "REPLICATED_RMS"}) == "CHAIN"
    assert reference_mode({"primary": "STANDALONE_RMS"}) == "STANDALONE_RMS"
    for values in (
        {"primary": "REPLICATED_RMS"},
        {"primary": "STANDALONE_RMS", "rms-a": "REPLICATED_RMS"},
        {"primary": "CHAIN", "rms-a": "STANDALONE_RMS"},
        {"primary": "unknown"},
    ):
        with pytest.raises(SyncError):
            reference_mode(values)


def test_standalone_migration_requires_normal_provisioning_recheck():
    from app.saas_admin.provisioning import additive_migration_refresh
    from app.tenancy.migrations import load_migrations

    entries = [(item.name, item.area, item.checksum) for item in load_migrations()]
    index = next(
        i
        for i, item in enumerate(entries)
        if item[0] == "20261010160000_tenant_standalone_primary.sql"
    )
    previous = hashlib.sha256(json.dumps(entries[:index]).encode()).hexdigest()
    assert not additive_migration_refresh(
        {"migrations": {"ok": True, "evidence": {"manifest_fingerprint": previous}}}
    )


@pytest.mark.parametrize("server_type,expected", [("STANDALONE_RMS", 200), ("CHAIN", 422)])
def test_primary_event_capture_verifies_live_type_and_releases_session(
    tmp_path, monkeypatch, server_type, expected
):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        assert request.url.host == "primary.iiko.example"
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text="test-token")
        if request.url.path.endswith("/serverType"):
            return httpx.Response(200, json=server_type)
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, content=META)
        return httpx.Response(200, content=xml(pair()))

    async def capture(connections, source_id, day):
        return await capture_events(connections, source_id, day, tmp_path / "events")

    monkeypatch.setattr(event_routes, "capture_events", capture)
    with TestClient(
        create_app(
            config(tmp_path, definitions=[], sync_api_key="test-sync-key"),
            iiko_transport=httpx.MockTransport(handle),
            connections_directory=tmp_path / "types",
        )
    ) as client:
        response = client.get(
            "/api/v1/iiko/connections/primary/events?date=2026-08-02", headers=HEADERS
        )
    assert response.status_code == expected
    assert calls[-1].endswith("/logout")
    assert sum(path.endswith("/serverType") for path in calls) == 1
    assert sum(path.endswith("/events") for path in calls) == (1 if expected == 200 else 0)
    if expected == 200:
        assert response.json()["source_id"] == "primary"
        assert response.json()["total"] == 2


def standalone_bundle(bundle):
    sources, snapshots = deepcopy(bundle)
    sources = sources[:1]
    groups = snapshots["rms-a", "groups"]
    groups["source_id"] = "primary"
    groups["payload"]["connection_id"] = "primary"
    snapshots = {
        key: value
        for key, value in snapshots.items()
        if key[0] == "primary" and key[1] != "replication"
    }
    snapshots["primary", "groups"] = groups
    snapshots["primary", "server_type"]["payload"]["server_type"] = "STANDALONE_RMS"
    return sources, snapshots


@pytest.mark.parametrize("mode", ["CHAIN", "STANDALONE_RMS", "mixed"])
def test_reference_collection_uses_real_topology_without_duplicate_or_replication_requests(
    empty_database, bundle, monkeypatch, tmp_path, mode
):
    operator, dsn, tenant = empty_database
    runtime = tenant()
    provision_tenant(operator, runtime)
    monkeypatch.setattr(sync_references, "ANALYTICS_SCHEMA", runtime.analytics_schema)
    sources, snapshots = standalone_bundle(bundle) if mode == "STANDALONE_RMS" else deepcopy(bundle)
    if mode == "mixed":
        snapshots["primary", "server_type"]["payload"]["server_type"] = "STANDALONE_RMS"
    monkeypatch.setattr(sync_references, "configured_sources", lambda _: sources)
    monkeypatch.setattr(
        sync_references,
        "capture_snapshot",
        lambda directory, source, resource, payload: snapshots[source.id, resource],
    )
    calls = []

    def handle(request):
        path = request.url.path.removeprefix("/api/v1/iiko")
        calls.append(path)
        if path == "/connections":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"connection_id": source.id, "base_url": source.base_url}
                        for source in sources
                    ]
                },
            )
        if path.endswith("/logout"):
            return httpx.Response(200, json={"state": "logged_out"})
        if path == "/stores/load":
            key = ("primary", "stores")
        elif path == "/replication/statuses":
            key = ("primary", "replication")
        else:
            _, _, source_id, resource = path.split("/")
            key = (
                source_id,
                {"server-type": "server_type", "corporate-groups": "groups"}.get(
                    resource, resource
                ),
            )
        return httpx.Response(200, json=snapshots[key]["payload"])

    monkeypatch.setattr(
        sync_references,
        "collector_client",
        lambda **kwargs: httpx.Client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    settings = config(tmp_path, database_url=make_conninfo(dsn, user=runtime.database_role))
    if mode == "mixed":
        with pytest.raises(SyncError, match="standalone_additional_sources_forbidden"):
            sync_references.synchronize(settings, "http://127.0.0.1:8010", tmp_path / "report.json")
        assert calls == [
            "/connections",
            "/connections/primary/server-type",
            "/connections/primary/logout",
        ]
        return
    report = sync_references.synchronize(
        settings, "http://127.0.0.1:8010", tmp_path / "report.json"
    )
    assert report["status"] == "succeeded"
    assert len(calls) == len(set(calls))
    assert ("/replication/statuses" in calls) == (mode == "CHAIN")
    assert ("/connections/primary/corporate-groups" in calls) == (mode == "STANDALONE_RMS")
    assert report["counts"]["raw_snapshots"] == (7 if mode == "CHAIN" else 4)
    assert report["logout_errors"] == []


def test_real_standalone_reference_binding_is_idempotent_scoped_and_required_for_events(
    empty_database, bundle, monkeypatch, tmp_path
):
    operator, dsn, tenant = empty_database
    first, other = tenant(), tenant()
    for runtime in (first, other):
        provision_tenant(operator, runtime)
    assert provision_tenant(operator, first) == ()
    monkeypatch.setattr(sync_references, "ANALYTICS_SCHEMA", first.analytics_schema)
    monkeypatch.setattr(sync_events, "ANALYTICS_SCHEMA", first.analytics_schema)
    monkeypatch.setattr(source_capabilities, "ANALYTICS_SCHEMA", first.analytics_schema)
    sources, snapshots = standalone_bundle(bundle)
    with psycopg.connect(make_conninfo(dsn, user=first.database_role), autocommit=True) as db:
        validate_database_runtime(db, first)
        sync_references.register_sources(db, sources)
        run_id = uuid4()
        db.execute(
            f"INSERT INTO {first.analytics_schema}.sync_runs(id,status) VALUES(%s,'running')",
            (run_id,),
        )
        for snapshot in snapshots.values():
            sync_references.append_snapshot(db, run_id, snapshot)
        counts = sync_references.publish(db, sources, snapshots)
        assert sync_references.publish(db, sources, snapshots) == counts
        assert counts["sources"] == 1 and counts["matched_rms"] == 1
        assert counts["replication_applicable"] is False
        assert counts["replication_status_counts"] is None
        assert db.execute(
            f"SELECT id,server_type FROM {first.analytics_schema}.sources"
        ).fetchall() == [("primary", "STANDALONE_RMS")]
        binding = db.execute(
            f"SELECT source_id,chain_source_id,details FROM {first.analytics_schema}.rms_bindings"
        ).fetchone()
        assert binding[:2] == ("primary", "primary")
        assert binding[2]["server_type"] == "STANDALONE_RMS"
        assert sync_events.event_sources(db, sources) == sources
        with pytest.raises(SyncError, match="standalone_additional_sources_forbidden"):
            sync_events.event_sources(
                db,
                sources
                + [Source("rms-x", "Duplicate", sources[0].base_url, sources[0].fingerprint)],
            )
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            db.execute(f"UPDATE {first.analytics_schema}.rms_bindings SET details='{{}}'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(f"SELECT * FROM {other.analytics_schema}.sources")
        broken = deepcopy(snapshots)
        broken["primary", "groups"]["payload"]["items"][0]["department_id"] = str(uuid4())
        with pytest.raises(SyncError, match="standalone_department_mapping_required"):
            sync_references.publish(db, sources, broken)
        assert sync_events.event_sources(db, sources) == sources
        exercise_event_history(first, dsn, sources, monkeypatch, tmp_path)
        db.execute(f"UPDATE {first.analytics_schema}.sources SET server_type='CHAIN'")
        with pytest.raises(SyncError, match="events_rms_required"):
            sync_events.event_sources(db, sources)
    assert (
        operator.execute(f"SELECT count(*) FROM {other.analytics_schema}.sources").fetchone()[0]
        == 0
    )


def exercise_event_history(runtime, dsn, sources, monkeypatch, tmp_path, *, check_unmapped=False):
    """Run actual history + event workers on real tenant SQL with synthetic HTTP/RAW."""
    monkeypatch.setattr(event_storage, "DB", runtime.analytics_schema)
    monkeypatch.setattr(sync_event_history, "ANALYTICS_SCHEMA", runtime.analytics_schema)
    monkeypatch.setattr(sync_event_history, "configured_sources", lambda _: sources)
    monkeypatch.setattr(sync_events, "configured_sources", lambda _: sources)
    monkeypatch.setattr(sync_events, "BACKEND_DIR", tmp_path)
    source = sources[-1]  # Replicated RMS after Chain, or sole standalone primary.
    folder = tmp_path / ".local" / "events" / source.id
    folder.mkdir(parents=True)
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"connection_id": item.id, "base_url": item.base_url} for item in sources
                    ]
                },
            )
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, json={"state": "logged_out"})
        assert request.url.path.endswith(f"/connections/{source.id}/events")
        raw, key = xml(pair()), uuid4()
        payload = EventsCapture(
            snapshot_id=key,
            source_id=source.id,
            date=DAY,
            received_at=datetime.now(UTC),
            total=2,
            sha256=hashlib.sha256(raw).hexdigest(),
            source_bytes=len(raw),
            metadata_sha256=hashlib.sha256(META).hexdigest(),
        ).model_dump(mode="json")
        (folder / f"{key}.xml").write_bytes(raw)
        (folder / f"{key}.metadata.xml").write_bytes(META)
        (folder / f"{key}.meta.json").write_text(
            json.dumps(payload | {"source_fingerprint": source.fingerprint})
        )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        sync_events,
        "collector_client",
        lambda **kwargs: httpx.Client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    settings = config(
        tmp_path,
        definitions=[],
        sync_api_key="test-sync-key",
        database_url=make_conninfo(dsn, user=runtime.database_role),
    )
    for _ in range(2):
        report = sync_event_history.synchronize_history(
            settings, DAY, DAY, Event(), directory=tmp_path / "history"
        )
        assert report["status"] == "succeeded"
        assert [item["source_id"] for item in report["sources"]] == [source.id]
    assert (
        sum(path.endswith("/events") for path in calls) == 1
    )  # Durable resume, no duplicate fetch.
    assert calls[-1].endswith("/logout")
    with psycopg.connect(make_conninfo(dsn, user=runtime.database_role)) as db:
        assert db.execute(
            f"SELECT source_id,event_count FROM {runtime.analytics_schema}.rms_event_days"
        ).fetchall() == [(source.id, 2)]
        assert (
            db.execute(f"SELECT count(*) FROM {runtime.analytics_schema}.rms_events").fetchone()[0]
            == 2
        )
    if check_unmapped:
        extra = Source("rms-unmapped", "Unmapped", "https://unmapped.example/resto/api", "b" * 64)
        sources.append(extra)
        with psycopg.connect(make_conninfo(dsn, user=runtime.database_role), autocommit=True) as db:
            sync_references.register_sources(db, [extra])
            db.execute(
                f"UPDATE {runtime.analytics_schema}.sources "
                "SET server_type='REPLICATED_RMS' WHERE id=%s",
                (extra.id,),
            )
        # The chosen matched RMS remains usable; unrelated incomplete mappings
        # still prevent a whole-fleet history from claiming complete coverage.
        assert (
            sync_events.synchronize_events(
                settings, EventsSyncQuery(source_id=source.id, date=DAY)
            )["status"]
            == "succeeded"
        )
        with pytest.raises(SyncError, match="events_rms_mapping_required"):
            sync_event_history.synchronize_history(
                settings, DAY, DAY, Event(), directory=tmp_path / "history"
            )
        assert sum(path.endswith("/events") for path in calls) == 2
        with psycopg.connect(make_conninfo(dsn, user=runtime.database_role)) as db:
            assert (
                db.execute(
                    f"SELECT count(*) FROM {runtime.analytics_schema}.rms_events"
                ).fetchone()[0]
                == 2
            )


def test_populated_chain_upgrade_preserves_rows_and_real_event_history(
    empty_database, bundle, monkeypatch, tmp_path
):
    operator, dsn, tenant = empty_database
    runtime = tenant()
    previous = tmp_path / "previous-migrations"
    shutil.copytree(MIGRATION_DIRECTORY, previous)
    manifest = json.loads((previous / "manifest.json").read_text())
    assert manifest["migrations"][-1]["file"] == "20261010160000_tenant_standalone_primary.sql"
    manifest["migrations"].pop()
    (previous / "manifest.json").write_text(json.dumps(manifest))
    provision_tenant(operator, runtime, directory=previous)
    monkeypatch.setattr(sync_references, "ANALYTICS_SCHEMA", runtime.analytics_schema)
    monkeypatch.setattr(sync_events, "ANALYTICS_SCHEMA", runtime.analytics_schema)
    monkeypatch.setattr(source_capabilities, "ANALYTICS_SCHEMA", runtime.analytics_schema)
    sources, snapshots = deepcopy(bundle)
    with psycopg.connect(make_conninfo(dsn, user=runtime.database_role), autocommit=True) as db:
        sync_references.register_sources(db, sources)
        run = uuid4()
        db.execute(
            f"INSERT INTO {runtime.analytics_schema}.sync_runs(id,status) VALUES(%s,'running')",
            (run,),
        )
        for snapshot in snapshots.values():
            sync_references.append_snapshot(db, run, snapshot)
        sync_references.publish(db, sources, snapshots)
        tables = ("sources", "rms_bindings", "raw_snapshots", "corporate_nodes", "stores")
        before = {
            table: db.execute(
                f"SELECT * FROM {runtime.analytics_schema}.{table} ORDER BY 1"
            ).fetchall()
            for table in tables
        }
        assert provision_tenant(operator, runtime) == (
            "20261010160000_tenant_standalone_primary.sql",
        )
        assert {
            table: db.execute(
                f"SELECT * FROM {runtime.analytics_schema}.{table} ORDER BY 1"
            ).fetchall()
            for table in tables
        } == before
        validate_database_runtime(db, runtime)
        assert sync_events.event_sources(db, sources) == sources[1:]
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            db.execute(
                f"UPDATE {runtime.analytics_schema}.rms_bindings SET chain_source_id=source_id"
            )
    exercise_event_history(runtime, dsn, sources, monkeypatch, tmp_path, check_unmapped=True)
