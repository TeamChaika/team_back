"""Verified immutable snapshot payloads. PostgreSQL retains identity and provenance."""

import base64
import gzip
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

MAX_OBJECT_BYTES = 128 * 1024 * 1024
MAX_ENVELOPE_BYTES = 512 * 1024 * 1024
PREFIX = "restcontrol/snapshots/v1"


class ArchiveError(RuntimeError):
    """Fixed public error code; never include provider exceptions or credentials."""


def validate_scope(scope):
    if scope != "chaika" and not re.fullmatch(r"c_[0-9a-f]{32}_analytics", scope):
        raise ArchiveError("snapshot_archive_scope_invalid")
    return scope


def object_key(scope, snapshot_id, digest):
    validate_scope(scope)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ArchiveError("snapshot_archive_digest_invalid")
    return f"{PREFIX}/{scope}/{UUID(str(snapshot_id))}/{digest}.json.gz"


@dataclass(frozen=True)
class ArchiveConfig:
    mode: str = "disabled"
    scope: str = "chaika"
    endpoint: str = ""
    bucket: str = ""
    region: str = "us-east-1"
    access_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    broker_socket: str = ""

    @classmethod
    def from_env(cls):
        if os.environ.get("RESTCONTROL_RUNTIME_MODE") == "tenant":
            from app.tenancy.config import load_runtime

            runtime = load_runtime()
            mode = os.environ.get("RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_MODE", "disabled")
            if (
                mode not in {"disabled", "broker"}
                or os.environ.get("CHAIKA_SNAPSHOT_ARCHIVE_MODE", "disabled") != "disabled"
                or os.environ.get("CHAIKA_SNAPSHOT_ARCHIVE_ACCESS_KEY")
                or os.environ.get("CHAIKA_SNAPSHOT_ARCHIVE_SECRET_KEY")
            ):
                raise ArchiveError("snapshot_archive_tenant_direct_forbidden")
            config = cls(
                mode=mode,
                scope=runtime.analytics_schema,
                broker_socket=os.environ.get(
                    "RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_BROKER_SOCKET", ""
                ),
            )
            validate_scope(config.scope)
            if mode == "broker" and not config.broker_socket.startswith("/"):
                raise ArchiveError("snapshot_archive_configuration_invalid")
            return config
        prefix = "CHAIKA_SNAPSHOT_ARCHIVE_"
        config = cls(
            **{
                name: os.environ.get(prefix + name.upper(), value.default)
                for name, value in cls.__dataclass_fields__.items()
            }
        )
        if config.mode not in {"disabled", "s3", "broker"}:
            raise ArchiveError("snapshot_archive_mode_invalid")
        validate_scope(config.scope)
        if config.mode == "s3":
            from urllib.parse import urlsplit

            parsed = urlsplit(config.endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
                or not all((config.bucket, config.access_key, config.secret_key))
            ):
                raise ArchiveError("snapshot_archive_configuration_invalid")
        if config.mode == "broker" and (
            not config.broker_socket.startswith("/") or config.access_key or config.secret_key
        ):
            raise ArchiveError("snapshot_archive_configuration_invalid")
        return config


@dataclass(frozen=True)
class ArchivePointer:
    object_key: str
    object_sha256: str
    object_bytes: int
    format_version: int = 1
    verified_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def encode_snapshot(snapshot, scope):
    validate_scope(scope)
    raw = bytes(snapshot["raw"])
    if ((len(raw) + 2) // 3) * 4 > MAX_ENVELOPE_BYTES:
        raise ArchiveError("snapshot_archive_size_limit")
    if hashlib.sha256(raw).hexdigest() != snapshot["sha256"]:
        raise ArchiveError("snapshot_archive_raw_integrity")
    envelope = {
        "version": 1,
        "scope": scope,
        "id": str(UUID(str(snapshot["id"]))),
        "source_id": snapshot["source_id"],
        "resource": snapshot["resource"],
        "sha256": snapshot["sha256"],
        "source_bytes": len(raw),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "normalized": snapshot["payload"],
    }
    encoded = bytearray()
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    for chunk in encoder.iterencode(envelope):
        piece = chunk.encode("utf-8")
        if len(encoded) + len(piece) > MAX_ENVELOPE_BYTES:
            raise ArchiveError("snapshot_archive_size_limit")
        encoded.extend(piece)
    result = gzip.compress(encoded, mtime=0)
    if len(result) > MAX_OBJECT_BYTES:
        raise ArchiveError("snapshot_archive_size_limit")
    return result


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError("snapshot_archive_envelope_invalid")
        result[key] = value
    return result


def decode_snapshot(data, scope, snapshot_id, source_id, resource, sha256, source_bytes):
    try:
        validate_scope(scope)
        if len(data) > MAX_OBJECT_BYTES:
            raise ArchiveError("snapshot_archive_size_limit")
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            decoded = stream.read(MAX_ENVELOPE_BYTES + 1)
        if len(decoded) > MAX_ENVELOPE_BYTES:
            raise ArchiveError("snapshot_archive_size_limit")
        payload = json.loads(
            decoded,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        expected = dict(
            version=1,
            scope=scope,
            id=str(UUID(str(snapshot_id))),
            source_id=source_id,
            resource=resource,
            sha256=sha256,
            source_bytes=source_bytes,
        )
        if any(payload.get(k) != v for k, v in expected.items()):
            raise ArchiveError("snapshot_archive_identity_mismatch")
        raw = base64.b64decode(payload["raw_base64"], validate=True)
        if len(raw) != source_bytes or hashlib.sha256(raw).hexdigest() != sha256:
            raise ArchiveError("snapshot_archive_raw_integrity")
        if not isinstance(payload["normalized"], (dict, list)):
            raise ArchiveError("snapshot_archive_envelope_invalid")
        return {"raw": raw, "payload": payload["normalized"]}
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError("snapshot_archive_envelope_invalid") from None


class S3Storage:
    def __init__(self, config):
        import boto3
        from botocore.config import Config

        self.config = config
        self.client = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 2},
                proxies={},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                s3={"addressing_style": "path"},
            ),
        )

    def put(self, key, data):
        try:
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=data,
                ContentType="application/gzip",
                ACL="private",
            )
        except Exception:
            raise ArchiveError("snapshot_archive_upload_failed") from None

    def get(self, key):
        body = None
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            body = response["Body"]
            if response["ContentLength"] > MAX_OBJECT_BYTES:
                raise ArchiveError("snapshot_archive_size_limit")
            data = body.read(MAX_OBJECT_BYTES + 1)
            if len(data) > MAX_OBJECT_BYTES or len(data) != response["ContentLength"]:
                raise ArchiveError("snapshot_archive_size_limit")
            return data
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("snapshot_archive_download_failed") from None
        finally:
            if body is not None:
                body.close()


class BrokerStorage:
    def __init__(self, config):
        self.config = config

    def _request(self, method, key, data=None):
        import httpx

        prefix = f"{PREFIX}/{self.config.scope}/"
        if not key.startswith(prefix) or not key.endswith(".json.gz"):
            raise ArchiveError("snapshot_archive_key_invalid")
        suffix = key[len(prefix) : -len(".json.gz")]
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=self.config.broker_socket),
                trust_env=False,
                timeout=90,
            ) as client:
                with client.stream(
                    method, f"http://archive/v1/snapshots/{suffix}", content=data
                ) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_OBJECT_BYTES:
                            raise ArchiveError("snapshot_archive_size_limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("snapshot_archive_broker_failed") from None

    def put(self, key, data):
        self._request("PUT", key, data)

    def get(self, key):
        return self._request("GET", key)


def storage(config):
    if config.mode == "s3":
        return S3Storage(config)
    if config.mode == "broker":
        return BrokerStorage(config)
    raise ArchiveError("snapshot_archive_unavailable")


def archive_snapshot(snapshot, config=None):
    config = config or ArchiveConfig.from_env()
    if config.mode == "disabled":
        return None
    data = encode_snapshot(snapshot, config.scope)
    digest = hashlib.sha256(data).hexdigest()
    key = object_key(config.scope, snapshot["id"], digest)
    client = storage(config)
    client.put(key, data)
    restored = client.get(key)
    if len(restored) != len(data) or hashlib.sha256(restored).hexdigest() != digest:
        raise ArchiveError("snapshot_archive_object_integrity")
    decode_snapshot(
        restored,
        config.scope,
        snapshot["id"],
        snapshot["source_id"],
        snapshot["resource"],
        snapshot["sha256"],
        len(snapshot["raw"]),
    )
    return ArchivePointer(key, digest, len(data))


def load_snapshot_payload(row, config=None):
    if row.get("raw") is not None and row.get("normalized") is not None:
        raw = bytes(row["raw"])
        if len(raw) != row["source_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ArchiveError("snapshot_archive_raw_integrity")
        return {"raw": raw, "payload": row["normalized"]}
    config = config or ArchiveConfig.from_env()
    key = object_key(config.scope, row["id"], row["payload_object_sha256"])
    if (
        row["payload_object_key"] != key
        or row["payload_format_version"] != 1
        or not row.get("payload_verified_at")
        or not 0 < row["payload_object_bytes"] <= MAX_OBJECT_BYTES
    ):
        raise ArchiveError("snapshot_archive_pointer_invalid")
    data = storage(config).get(key)
    if (
        len(data) != row["payload_object_bytes"]
        or hashlib.sha256(data).hexdigest() != row["payload_object_sha256"]
    ):
        raise ArchiveError("snapshot_archive_object_integrity")
    return decode_snapshot(
        data,
        config.scope,
        row["id"],
        row["source_id"],
        row["resource"],
        row["sha256"],
        row["source_bytes"],
    )
