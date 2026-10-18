"""Startup archive proof uses readonly binary reads and never logs payloads."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from app import archive_verify as verify
from app import serve
from app import snapshot_archive as archive
from app.core import config as settings


@pytest.fixture(autouse=True)
def disabled(monkeypatch):
    monkeypatch.delenv("CHAIKA_ARCHIVE_VERIFY_SNAPSHOT_IDS", raising=False)


@pytest.fixture
def runtime(monkeypatch):
    config = archive.ArchiveConfig(mode="s3", scope="chaika")
    monkeypatch.setattr(archive.ArchiveConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        settings, "Settings", lambda: SimpleNamespace(database_url=SecretStr("secret-dsn"))
    )
    rows, objects = [], {}
    for external in (True, False):
        raw = b"private-payload-token\x00\xff"
        row = dict(
            id=uuid4(),
            source_id="private-source",
            resource="accounts",
            sha256=hashlib.sha256(raw).hexdigest(),
            source_bytes=len(raw),
            raw=raw,
            normalized={"secret": "never-print"},
            payload_object_key=None,
            payload_object_sha256=None,
            payload_object_bytes=None,
            payload_format_version=None,
            payload_verified_at=None,
        )
        if external:
            data = archive.encode_snapshot(dict(row, payload=row["normalized"]), "chaika")
            digest = hashlib.sha256(data).hexdigest()
            key = archive.object_key("chaika", row["id"], digest)
            objects[key] = data
            row.update(
                raw=None,
                normalized=None,
                payload_object_key=key,
                payload_object_sha256=digest,
                payload_object_bytes=len(data),
                payload_format_version=1,
                payload_verified_at=datetime.now(UTC),
            )
        rows.append(row)
    reads = []

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params, **kwargs):
            assert query.startswith("SELECT ") and "FROM chaika.raw_snapshots WHERE id=%s" in query
            assert "*" not in query and kwargs == {"binary": True}
            reads.append(str(params[0]))
            return SimpleNamespace(
                fetchone=lambda: next((row for row in rows if row["id"] == params[0]), None)
            )

    def connect(dsn, **kwargs):
        assert dsn == "secret-dsn"
        assert kwargs["autocommit"] is True and kwargs["connect_timeout"] == 10
        assert (
            kwargs["options"]
            == "-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000"
        )
        return Database()

    class Storage:
        def get(self, key):
            return objects[key]

        def put(self, *args):
            pytest.fail("Startup proof must not write S3")

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(archive, "storage", lambda _: Storage())
    monkeypatch.setenv(
        "CHAIKA_ARCHIVE_VERIFY_SNAPSHOT_IDS", ",".join(str(row["id"]) for row in rows)
    )
    return rows, objects, reads


def test_disabled_has_no_database_storage_or_output(monkeypatch, capsys):
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("disabled DB"))
    monkeypatch.setattr(archive, "storage", lambda *a: pytest.fail("disabled S3"))
    verify.verify_archive_startup()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["bad", str(uuid4()), ",".join([str(uuid4())] * 2), "x" * 81])
def test_invalid_ids_fail_closed_before_database(monkeypatch, capsys, value):
    monkeypatch.setenv("CHAIKA_ARCHIVE_VERIFY_SNAPSHOT_IDS", value)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("invalid IDs DB"))
    with pytest.raises(SystemExit):
        verify.verify_archive_startup()
    assert "archive_runtime_verification_failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "mode,scope", [("disabled", "chaika"), ("broker", "chaika"), ("s3", "other")]
)
def test_wrong_archive_mode_or_scope_never_connects(runtime, monkeypatch, mode, scope):
    monkeypatch.setattr(
        archive.ArchiveConfig, "from_env", lambda: archive.ArchiveConfig(mode=mode, scope=scope)
    )
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("wrong config DB"))
    with pytest.raises(SystemExit):
        verify.verify_archive_startup()


def test_real_reader_verifies_both_storage_modes_and_safe_hashes(runtime, capsys, monkeypatch):
    monkeypatch.setenv("COMMIT_SHA", "forged-commit-secret")
    rows, _, reads = runtime
    verify.verify_archive_startup()
    output = capsys.readouterr().out
    result = json.loads(output.removeprefix(verify.PREFIX))
    assert result["status"] == "verified"
    assert reads == [str(row["id"]) for row in rows]
    assert {row["storage"] for row in result["snapshots"]} == {"inline", "external"}
    assert all(row["verified"] is True for row in result["snapshots"])
    assert set(result["module_sha256"]) == {"app/" + name for name in verify.MODULES}
    assert all(len(value) == 64 for value in result["module_sha256"].values())
    assert (
        datetime.fromisoformat(result["verification_started_at"]).utcoffset().total_seconds() == 0
    )
    assert all(
        secret not in output
        for secret in ("secret-dsn", "private-payload", "never-print", "forged-commit")
    )


@pytest.mark.parametrize("which", ["inline", "external"])
def test_integrity_failure_emits_only_fixed_failure(runtime, capsys, which):
    rows, objects, _ = runtime
    if which == "inline":
        rows[1]["raw"] = b"secret-corruption"
    else:
        objects[rows[0]["payload_object_key"]] = b"secret-corruption"
    with pytest.raises(SystemExit):
        verify.verify_archive_startup()
    assert json.loads(capsys.readouterr().out.removeprefix(verify.PREFIX)) == {
        "status": "failed",
        "code": "archive_runtime_verification_failed",
    }


def test_two_inline_rows_do_not_claim_external_reader_proof(runtime):
    rows, _, _ = runtime
    rows[0].update(raw=rows[1]["raw"], normalized=rows[1]["normalized"])
    with pytest.raises(SystemExit):
        verify.verify_archive_startup()


def test_opted_in_failure_prevents_http_start(runtime, monkeypatch):
    runtime[0][1]["raw"] = b"bad"
    monkeypatch.setattr(
        serve.uvicorn, "run", lambda *a, **k: pytest.fail("HTTP started after failed proof")
    )
    with pytest.raises(SystemExit):
        serve.main()
