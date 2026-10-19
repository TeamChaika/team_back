"""Durable cache contracts against a disposable local PostgreSQL, never TEST_DATABASE_URL."""

import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.web.indicator_cache import PostgresIndicatorCache
from app.web.indicators import (
    BASE,
    EXTRA,
    METRIC_INPUTS,
    IndicatorQuery,
    IndicatorService,
    calculate,
)
from app.web.sync_policy import SyncPolicy

USER, DEPT, OTHER = UUID(int=1), UUID(int=2), UUID(int=3)


@pytest.fixture(scope="module")
def local_database():
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("Disposable local PostgreSQL binaries are unavailable")
    with tempfile.TemporaryDirectory(prefix="indicator-pg-", dir="/tmp") as directory:
        data = str(Path(directory) / "data")
        subprocess.run(
            [initdb, "-D", data, "-A", "trust", "--no-locale", "--encoding=UTF8"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                pg_ctl,
                "-D",
                data,
                "-l",
                str(Path(directory) / "server.log"),
                "-o",
                f"-F -k {directory} -h ''",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        try:
            dsn = f"host={directory} dbname=postgres"
            with psycopg.connect(dsn, autocommit=True) as db:
                for role in ("anon", "authenticated", "chaika_backend"):
                    db.execute(f"CREATE ROLE {role}")
                db.execute("CREATE SCHEMA chaika")
                db.execute(
                    Path("supabase/migrations/20261019150000_indicator_cache.sql").read_text()
                )
            yield dsn
        finally:
            subprocess.run(
                [pg_ctl, "-D", data, "-m", "immediate", "-w", "stop"],
                check=True,
                capture_output=True,
            )


@pytest.fixture
def cache(local_database):
    with psycopg.connect(local_database) as db:
        db.execute("TRUNCATE chaika.indicator_cache")
    return PostgresIndicatorCache(SimpleNamespace(database_url=SecretStr(local_database)))


class Repository:
    def __init__(self):
        self.allowed = True
        self.departments = [DEPT]
        self.stores = None
        self.policy = SyncPolicy()

    def scope(self, user_id=USER, selected=()):
        if not self.allowed:
            raise HTTPException(403, "revoked")
        return SimpleNamespace(
            user={"id": user_id, "sections": ["indicators"]},
            ids=list(selected) or self.departments,
            selection_ids=selected,
            store_ids=self.stores or (),
            warehouse_restricted=self.stores is not None,
        )

    def indicator_warm_scopes(self):
        if self.allowed:
            yield self.scope()

    def sync_policy(self):
        return self.policy


def payload():
    return {
        "values": [calculate({"revenue": 900, "cost": 300})] * 2,
        "observed_at": datetime.now(UTC),
    }


@pytest.fixture
def services(cache):
    active = []

    def make(repo=None, fetch=None):
        repo = repo or Repository()
        service = IndicatorService(
            None,
            fetch=fetch or (lambda *_a, **_k: payload()),
            store=cache,
            repository=repo,
            policy_reader=repo.sync_policy,
        )
        active.append(service)
        return service

    yield make
    for service in active:
        service.close()


def drain(service):
    service.executor.submit(lambda: None).result(timeout=5)


def query():
    return IndicatorQuery(day=datetime.now(UTC).date())


def expire(cache):
    with cache.connection() as db:
        db.execute(
            "UPDATE chaika.indicator_cache SET due_at=now()-interval '1 second',"
            "observed_at=now()-interval '6 minutes'"
        )


def test_background_without_visits_and_restart_reads_last_good(cache, services):
    calls = []

    def fetch(*args):
        calls.append(args)
        return payload()

    service = services(fetch=fetch)
    service.tick()
    drain(service)
    assert len(calls) == 1
    assert set(calls[0][3]) == set(BASE | EXTRA)
    expire(cache)
    service.tick()
    drain(service)
    assert len(calls) == 2
    second = services(fetch=lambda *_: pytest.fail("Fresh cache must not fetch"))
    report = second.get(second.repository.scope(), query(), "revenue")
    assert report["status"] == "ready"
    assert report["current"]["value"] == 900
    assert not report["stale"]


def test_expired_read_immediate_and_all_metrics_singleflight(cache, services):
    initial = services()
    initial.tick()
    drain(initial)
    expire(cache)
    entered, release = Event(), Event()
    calls = []

    def fetch(*_):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return payload()

    service = services(fetch=fetch)
    try:
        report = service.get(service.repository.scope(), query(), "revenue")
        assert report["stale"] and report["status"] == "ready"
        assert entered.wait(2)
        for metric in METRIC_INPUTS:
            assert service.get(service.repository.scope(), query(), metric)["status"] == "ready"
        other = services(fetch=fetch)
        other.tick()
        drain(other)
        assert calls == [1]
    finally:
        release.set()
    drain(service)
    assert not service.get(service.repository.scope(), query(), "revenue")["stale"]


def test_failed_refresh_retains_payload_and_time(cache, services):
    service = services()
    service.tick()
    drain(service)
    expire(cache)
    before = cache.due()[0]

    def fail(*_):
        raise ValueError("private provider details")

    failing = services(fetch=fail)
    failing.tick()
    drain(failing)
    after = cache.read(before["key"])
    assert after["payload"] == before["payload"]
    assert after["observed_at"] == before["observed_at"]
    report = failing.get(failing.repository.scope(), query(), "revenue")
    assert report["stale"] and report["current"]["value"] == 900
    assert "private" not in json.dumps(report, default=str)


def test_background_reauthorizes_and_policy_changes_do_not_reuse(cache, services):
    repo = Repository()
    service = services(repo=repo)
    service.tick()
    drain(service)
    expire(cache)
    key = cache.due()[0]["key"]
    repo.allowed = False
    service.tick()
    drain(service)
    assert cache.read(key) is None
    repo.allowed = True
    service.tick()
    service.get(repo.scope(), query(), "revenue")
    drain(service)
    repo.policy = SyncPolicy(1)
    assert service.get(repo.scope(), query(), "revenue")["status"] == "loading"
    drain(service)
    repo.stores = [OTHER]
    assert service.get(repo.scope(), query(), "revenue")["status"] == "loading"
    drain(service)


def test_stale_lease_cannot_publish_and_capacity_is_bounded(cache, monkeypatch):
    from app.web import indicator_cache

    monkeypatch.setattr(indicator_cache, "MAX_ENTRIES", 3)
    for number in range(10):
        assert cache.register(f"{number:064x}", {}, USER, [])
    with cache.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM chaika.indicator_cache").fetchone()["n"] == 3
    key = f"{9:064x}"
    token = cache.claim(key)
    assert cache.claim(key) is None
    cache.success(key, UUID(int=999), payload())
    assert cache.read(key)["payload"] is None
    cache.success(key, token, payload())
    assert cache.read(key)["payload"] is not None


def test_start_runs_without_requests_and_unknown_session_stays_quarantined(cache, services):
    from app.integrations.iiko.errors import IikoError

    called = Event()
    calls = []

    def fail(*_):
        calls.append(1)
        called.set()
        raise IikoError("iiko_session_unknown", "private")

    service = services(fetch=fail)
    service.start()
    assert called.wait(3)
    drain(service)
    assert service.session_unknown
    with cache.connection() as db:
        db.execute("UPDATE chaika.indicator_cache SET due_at=now()-interval '1 second'")
    service.tick()
    drain(service)
    assert calls == [1]


def test_expired_lease_cannot_publish(cache):
    key = "a" * 64
    cache.register(key, {}, USER, [])
    token = cache.claim(key)
    with cache.connection() as db:
        db.execute("UPDATE chaika.indicator_cache SET lease_until=now()-interval '1 second'")
    cache.success(key, token, payload())
    assert cache.read(key)["payload"] is None
    replacement = cache.claim(key)
    assert replacement and replacement != token
    cache.success(key, replacement, payload())
    assert cache.read(key)["payload"] is not None


def test_same_query_in_separate_databases_never_shares_payload(local_database, cache):
    # Runtime database boundary, not a claim that SaaS schema rewriting is verified here.
    with psycopg.connect(local_database, autocommit=True) as db:
        db.execute("CREATE DATABASE indicator_other")
    dsn = local_database.replace("dbname=postgres", "dbname=indicator_other")
    try:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("CREATE SCHEMA chaika")
            db.execute(Path("supabase/migrations/20261019150000_indicator_cache.sql").read_text())
        second = PostgresIndicatorCache(SimpleNamespace(database_url=SecretStr(dsn)))
        key = "b" * 64
        cache.register(key, {}, USER, [])
        cache.success(key, cache.claim(key), payload())
        assert second.read(key) is None
        second.register(key, {}, USER, [])
        different = payload()
        different["values"] = [calculate({"revenue": 123})] * 2
        second.success(key, second.claim(key), different)
        assert second.read(key)["payload"]["values"][0]["revenue"] == 123
        assert cache.read(key)["payload"]["values"][0]["revenue"] == 900
    finally:
        with psycopg.connect(local_database, autocommit=True) as db:
            db.execute("DROP DATABASE indicator_other")


def test_explicit_cleared_default_filters_survive_background_roundtrip(cache, services):
    calls = []

    def fetch(_settings, requested, *_args):
        calls.append(requested.effective_filters())
        return payload()

    service = services(fetch=fetch)
    cleared = IndicatorQuery(day=query().start, filters={"dish_deleted": [], "order_deleted": []})
    assert service.get(service.repository.scope(), cleared, "revenue")["status"] == "loading"
    drain(service)
    report = service.get(service.repository.scope(), cleared, "revenue")
    assert report["status"] == "ready"
    assert calls == [{}]
    assert (
        service._spec(service.repository.scope(), cleared)[0]
        != service._spec(service.repository.scope(), query())[0]
    )


def test_eviction_between_registration_and_read_returns_loading(cache, services, monkeypatch):
    from app.web import indicator_cache

    monkeypatch.setattr(indicator_cache, "MAX_ENTRIES", 1)
    real_read = cache.read
    reads = 0

    def interleaved_read(key):
        nonlocal reads
        reads += 1
        if reads == 2:
            # Simulate another process admitting a different query at full capacity.
            cache.register("c" * 64, {}, USER, [])
        return real_read(key)

    monkeypatch.setattr(cache, "read", interleaved_read)
    service = services()
    assert service.get(service.repository.scope(), query(), "revenue")["status"] == "loading"
    monkeypatch.setattr(cache, "read", real_read)
    assert service.get(service.repository.scope(), query(), "revenue")["status"] == "loading"
    drain(service)
    assert service.get(service.repository.scope(), query(), "revenue")["status"] == "ready"


def test_archive_revision_invalidates_payload_and_background_skips_archived(cache, services):
    repo = Repository()
    repo.departments = [DEPT, OTHER]
    fetched = []
    service = services(repo, lambda *args: fetched.append(args[2]) or payload())
    service.tick()
    drain(service)
    old_key, _ = service._spec(repo.scope(), query())
    repo.policy = SyncPolicy(1, None, frozenset([str(OTHER)]))
    new_key, spec = service._spec(repo.scope(), query())
    assert old_key != new_key and spec["departments"] == [str(DEPT)]
    assert service.get(repo.scope(), query())["status"] == "loading"
    drain(service)
    assert fetched[-1] == [str(DEPT)]
    with pytest.raises(HTTPException):
        service._spec(repo.scope(selected=(OTHER,)), query())
    with_archives = query().model_copy(update={"include_archived": True})
    archive_key, spec = service._spec(repo.scope(), with_archives)
    assert archive_key != new_key and spec["departments"] == [str(DEPT), str(OTHER)]
    repo.policy = SyncPolicy(2, None, frozenset([str(DEPT), str(OTHER)]))
    service.last_warm = None
    before = len(fetched)
    expire(cache)
    service.tick()
    drain(service)
    assert len(fetched) == before
    with pytest.raises(HTTPException):
        service.get(repo.scope(), query())
