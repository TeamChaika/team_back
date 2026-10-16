import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.sync_references import Source, append_snapshot, reference_lock, register_sources
from app.sync_scope import load_scope
from app.sync_store_balances import publish_store_balances
from app.tenancy.connection import tenant_connect
from app.tenancy.sql import ANALYTICS_SCHEMA as S

TENANT_INDEX = 0

D1, D2, A, B, P = [UUID(int=n) for n in range(1, 6)]
with (
    tenant_connect(Settings().database_url.get_secret_value(), autocommit=True) as db,
    reference_lock(db),
):
    register_sources(
        db, [Source("primary", "Tenant", "https://tenant.example/resto/api", "a" * 64)]
    )
    run = uuid4()
    db.execute(f"INSERT INTO {S}.sync_runs(id,status) VALUES(%s,'running')", (run,))

    def snapshot(resource, items, seconds):
        key = uuid4()
        now = datetime.now(UTC) + timedelta(seconds=seconds)
        payload = dict(
            snapshot_id=str(key),
            received_at=now.isoformat(),
            request={"timestamp": "2026-09-09T23:59:59"},
            items=items,
            total=len(items),
            source_bytes=2,
            sha256=hashlib.sha256(b"[]").hexdigest(),
        )
        snap = dict(
            id=str(key),
            source_id="primary",
            resource=resource,
            observed_at=now,
            raw=b"[]",
            sha256=payload["sha256"],
            payload=payload,
        )
        append_snapshot(db, run, snap)
        return snap

    seed = snapshot("departments", [], 0)
    for d, store in [(D1, A), (D2, B)]:
        db.execute(
            f"INSERT INTO {S}.corporate_nodes(source_id,id,type,name,fir"
            f"st_seen_at,last_seen_at,last_snapshot_id) VALUES('primary'"
            f",%s,'DEPARTMENT','Own',%s,%s,%s)",
            (d, seed["observed_at"], seed["observed_at"], seed["id"]),
        )
        db.execute(
            f"INSERT INTO {S}.stores(source_id,id,parent_id,name,first_s"
            f"een_at,last_seen_at,last_snapshot_id) VALUES('primary',%s,"
            f"%s,'Own store',%s,%s,%s)",
            (store, d, seed["observed_at"], seed["observed_at"], seed["id"]),
        )
    first = snapshot(
        "store_balances",
        [dict(store_id=str(store), product_id=str(P), amount="1", sum="10") for store in [A, B]],
        1,
    )
    publish_store_balances(db, first)
    selected = [str(D1)] if TENANT_INDEX == 0 else []
    policy = {
        "sync": {"mode": "selected", "department_ids": selected},
        "writeoff": {"mode": "all", "store_ids": []},
        "waybill": {"mode": "all", "store_ids": []},
    }
    db.execute(
        f"INSERT INTO {S}.operation_settings(singleton,revision,settings"
        f",updated_by) VALUES(true,1,%s,%s)",
        (Jsonb(policy), uuid4()),
    )
    assert load_scope(db).departments == frozenset(selected)
    second = snapshot(
        "store_balances",
        [dict(store_id=str(store), product_id=str(P), amount="9", sum="90") for store in [A, B]],
        2,
    )
    publish_store_balances(db, second)
    rows = db.execute(
        f"SELECT i.store_id,i.amount,i.origin_snapshot_id FROM {S}.store"
        f"_balance_reports r JOIN {S}.store_balance_items i ON i.snapsho"
        f"t_id=r.last_snapshot_id"
    ).fetchall()
    values = {str(store): amount for store, amount, origin in rows}
    assert values[str(B)] == 1
    assert values[str(A)] == (9 if TENANT_INDEX == 0 else 1)
    if TENANT_INDEX == 0:
        assert next(origin for store, amount, origin in rows if store == B) == UUID(first["id"])
