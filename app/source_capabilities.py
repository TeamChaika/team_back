"""Require published evidence before treating standalone RMS as a primary source."""

from app.sync_references import Source, SyncError
from app.tenancy.sql import ANALYTICS_SCHEMA


def require_primary_source(db, source_id="primary", *, sources: list[Source] | None = None) -> str:
    row = db.execute(
        f"SELECT server_type, configured, verified_at FROM {ANALYTICS_SCHEMA}.sources WHERE id=%s",
        (source_id,),
    ).fetchone()
    if not row or row[0] not in {"CHAIN", "STANDALONE_RMS"}:
        raise SyncError("reference_sync_required")
    if row[0] == "CHAIN":
        return row[0]
    if sources is not None and (len(sources) != 1 or sources[0].id != source_id):
        raise SyncError("standalone_additional_sources_forbidden")
    if db.execute(
        f"SELECT 1 FROM {ANALYTICS_SCHEMA}.sources WHERE configured AND id<>%s LIMIT 1",
        (source_id,),
    ).fetchone():
        raise SyncError("standalone_additional_sources_forbidden")
    if source_id != "primary" or not row[1] or row[2] is None:
        raise SyncError("reference_sync_required")
    if not db.execute(
        f"SELECT 1 FROM {ANALYTICS_SCHEMA}.rms_bindings "
        "WHERE source_id=%s AND chain_source_id=%s AND state='matched' "
        "AND details->>'server_type'='STANDALONE_RMS' "
        "AND chain_snapshot_id=rms_snapshot_id",
        (source_id, source_id),
    ).fetchone():
        raise SyncError("reference_sync_required")
    return row[0]
