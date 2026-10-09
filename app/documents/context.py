"""Immutable document-company context; never chosen from an HTTP host or payload."""

from zoneinfo import ZoneInfo

from app.tenancy.config import TenantRuntime, load_runtime
from app.tenancy.locks import advisory_lock_key
from app.tenancy.sql import schema_identifier


def runtime_of(value=None) -> TenantRuntime:
    if isinstance(value, TenantRuntime):
        return value
    runtime = getattr(value, "tenant_runtime", None)
    if not isinstance(runtime, TenantRuntime):
        runtime = getattr(value, "runtime", None)
    return runtime if isinstance(runtime, TenantRuntime) else load_runtime()


def analytics_schema(db=None) -> str:
    return schema_identifier("analytics", runtime_of(db))


def local_zone(value=None) -> ZoneInfo:
    runtime = runtime_of(value)
    return ZoneInfo(runtime.timezone if runtime.mode == "tenant" else "Europe/Simferopol")


def lock_resource(db, resource: str) -> str:
    runtime = runtime_of(db)
    return f"{runtime.key}:{resource}" if runtime.mode == "tenant" else resource


def fixed_lock(db, purpose: str, legacy: int) -> int:
    runtime = runtime_of(db)
    return advisory_lock_key(runtime, purpose) if runtime.mode == "tenant" else legacy
