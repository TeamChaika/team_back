"""Stable cross-process lock keys. Python's randomized hash() is deliberately unused."""

import hashlib
import json

from app.tenancy.config import TenantRuntime


def advisory_lock_key(runtime: TenantRuntime, purpose: str, resource: str = "") -> int:
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("Lock purpose is required")
    if not isinstance(resource, str):
        raise ValueError("Lock resource must be a string")
    payload = json.dumps(
        ["restcontrol-lock-v1", runtime.key, purpose, resource],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)
