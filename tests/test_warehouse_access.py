"""Warehouse authorization ceilings, real scope construction and native SQL ACL."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.commercial_invoices.policy import stores_for as commercial_stores
from app.core.config import Settings
from app.documents.policy import stores_for as document_stores
from app.documents.reads import options as document_options
from app.documents.workflow import has_receipt_approver
from app.portal import create_portal
from app.web.administration import EditAccount
from app.web.assistant_store import access_hash
from app.web.permissions import SECTIONS, require_warehouse_section
from app.web.repository import Repository
from app.web.settings import WebSettings
from tests.test_portal import USER, FakeRepository, login, provider
from tests.test_restaurant_selection import NESTED, SA, SB, SC, A, B, C, PermissionDatabase, Rows


class WarehouseDatabase(PermissionDatabase):
    mode = "selected"
    warehouse_ids = (SA,)
    departments = ()
    owner = False

    def execute(self, sql, params=None):
        if "chaika.web_users" in sql:
            return Rows(
                [
                    {
                        "id": USER,
                        "display_name": "Test",
                        "role": "owner" if self.owner else "manager",
                        "warehouse_scope_mode": self.mode,
                        "all_departments": self.owner,
                    }
                ]
            )
        if "chaika.web_warehouse_access" in sql:
            return Rows([{"store_id": store} for store in self.warehouse_ids])
        if "chaika.web_department_access" in sql:
            return Rows([{"department_id": department} for department in self.departments])
        return super().execute(sql, params)


def warehouse_repo():
    db = WarehouseDatabase()
    repo = object.__new__(Repository)

    @contextmanager
    def connection(**kwargs):
        yield db

    repo.connection = connection
    return repo, db


def test_selected_warehouse_derives_navigation_without_venue_authority():
    repo, db = warehouse_repo()
    scope = repo.scope(USER)
    assert scope.ids == [A]
    assert scope.store_ids == (SA,)
    assert NESTED not in scope.store_ids
    assert scope.warehouse_restricted and not scope.unrestricted
    with pytest.raises(HTTPException) as denied:
        repo.scope(USER, B)
    assert denied.value.status_code == 403
    db.owner = True
    assert repo.scope(USER).store_ids == (SA,)
    assert not repo.scope(USER).unrestricted


def test_explicit_department_selection_can_only_narrow():
    repo, db = warehouse_repo()
    db.warehouse_ids = (SA, SB, SC)
    db.departments = (A, B)
    assert set(repo.scope(USER).store_ids) == {SA, SB}
    assert repo.scope(USER, B).store_ids == (SB,)
    with pytest.raises(HTTPException):
        repo.scope(USER, C)


def test_empty_and_unknown_grants_never_become_full_access():
    repo, db = warehouse_repo()
    for stores in [(), (UUID(int=9999),)]:
        db.warehouse_ids = stores
        scope = repo.portal_scope(USER)
        assert not scope.store_ids and not scope.departments and not scope.unrestricted
        assert repo.metadata(scope)["sales_dates"] == []
        with pytest.raises(HTTPException):
            require_warehouse_section(scope, "balances")


def test_selected_metadata_does_not_read_global_dates():
    repo, _ = warehouse_repo()
    assert repo.metadata(repo.scope(USER))["sales_dates"] == []
    assert repo.metadata(repo.scope(USER))["balance_dates"] == []


def test_existing_all_warehouse_mode_and_document_guards_preserved():
    repo, db = warehouse_repo()
    db.mode, db.owner = "all", True
    scope = repo.scope(USER)
    assert scope.unrestricted and {SA, SB, SC, NESTED} == set(scope.store_ids)
    db.mode = "selected"
    restricted = repo.scope(USER)
    invoice = repo.resource_query(restricted, "invoices")
    assert invoice["params"] == [[SA]] * 3
    assert (
        "AND NOT EXISTS" in invoice["guard"]
        and "COALESCE(i.store_id,t.default_store_id)" in invoice["guard"]
    )
    transfer = repo.resource_query(restricted, "transfers")
    assert "OR t.store_to_id" in transfer["guard"]
    assert transfer["params"] == [[SA], [SA]]


def test_account_schema_selected_empty_is_explicit_deny_and_no_department_required():
    payload = EditAccount(
        display_name="Test",
        sections=["sales"],
        revision=1,
        warehouse_scope_mode="selected",
        warehouse_ids=[],
    )
    assert payload.warehouse_ids == [] and not payload.department_ids
    for fields in [
        dict(warehouse_scope_mode="selected"),
        dict(warehouse_ids=[SA]),
        dict(warehouse_scope_mode="all", warehouse_ids=[SA]),
        dict(warehouse_scope_mode="selected", warehouse_ids=[SA, SA]),
    ]:
        with pytest.raises(ValidationError):
            EditAccount(display_name="Test", sections=[], revision=1, **fields)
    legacy = EditAccount(display_name="Test", sections=[], revision=1)
    assert legacy.warehouse_scope_mode is None and legacy.warehouse_ids is None


def test_scope_hash_changes_on_mode_grants_and_section_revocation():
    repo, db = warehouse_repo()
    selected = repo.scope(USER)
    all_mode = replace(selected, user={**selected.user, "warehouse_scope_mode": "all"})
    revoked = replace(selected, store_ids=())
    hidden = replace(selected, user={**selected.user, "sections": []})
    assert len({access_hash(value) for value in [selected, all_mode, revoked]}) == 3
    visible = replace(selected, user={**selected.user, "sections": ["purchase-prices"]})
    assert access_hash(hidden) != access_hash(visible)


class RestrictedHTTPRepository(FakeRepository):
    def scope(self, *args):
        original = super().scope(*args)
        return replace(
            original,
            user={
                **original.user,
                "role": "owner",
                "all_departments": True,
                "warehouse_scope_mode": "selected",
                "sections": list(SECTIONS),
            },
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/resources/products",
        "/api/resources/charts",
        "/api/resources/cash-shifts",
        "/api/resources/employees",
        "/api/resources/events",
        "/api/status",
        "/api/employees/options",
        "/api/employees/pending",
        "/api/topology",
        "/api/deposits",
        "/api/deposits/export",
        "/api/deposits/permissions",
    ],
)
def test_direct_endpoint_blocks_unsupported_even_for_owner(path):
    app = create_portal(
        Settings(_env_file=None),
        WebSettings(_env_file=None, anon_key="test"),
        repository=RestrictedHTTPRepository(),
        auth_transport=httpx.MockTransport(provider),
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        assert login(client).status_code == 200
        assert client.get(path).status_code == 403
        me = client.get("/api/me").json()
        assert me["warehouse_scope"]["mode"] == "selected"
        assert "deposits" in me["warehouse_capabilities"]["unsupported_sections"]


def test_native_and_commercial_sql_intersection_and_active_identity():
    pgserver = pytest.importorskip("pgserver")
    with TemporaryDirectory(prefix="chaika-warehouse-acl-") as directory:
        server = pgserver.get_server(Path(directory) / "postgres", cleanup_mode="stop")
        try:
            with psycopg.connect(server.get_uri(), autocommit=True, row_factory=dict_row) as db:
                db.execute("CREATE ROLE chaika_backend")
                db.execute("CREATE ROLE chaika_iiko_app")
                db.execute("CREATE SCHEMA chaika")
                db.execute("CREATE TABLE chaika.web_users(id uuid PRIMARY KEY,active boolean)")
                db.execute(
                    "CREATE TABLE chaika.stores(source_id text,id uuid,PRIMARY KEY(source_id,id))"
                )
                db.execute(
                    Path("supabase/migrations/20261007120000_warehouse_access.sql").read_text()
                )
                db.execute(Path("migrations/documents/0010_global_warehouse_scope.sql").read_text())
                db.execute(
                    "CREATE TABLE portal_documents_userlink(user_id bigint,supabase_id uuid)"
                )
                db.execute(
                    "CREATE TABLE portal_documents_grant(user_id bigint,kind text,"
                    "store_id uuid,actions jsonb)"
                )
                db.execute("CREATE TABLE commercial_invoice_grants(LIKE portal_documents_grant)")
                db.execute("CREATE VIEW stores AS SELECT id,id::text AS name FROM chaika.stores")
                db.execute("CREATE TABLE writeoffs_reasons(id bigint,name text,account_id uuid)")
                db.execute(
                    "CREATE TABLE authentication_user(id bigint,is_active boolean,"
                    "telegram_id bigint)"
                )
                db.execute("INSERT INTO authentication_user VALUES(1,true,NULL)")
                db.execute(
                    "CREATE VIEW portal_access AS SELECT id,active,"
                    "'[\"transfers\"]'::jsonb AS sections FROM chaika.web_users"
                )

                db.execute("INSERT INTO chaika.web_users(id,active) VALUES(%s,true)", (USER,))
                db.execute(
                    "INSERT INTO chaika.stores VALUES('primary',%s),('primary',%s)", (SA, SB)
                )
                db.execute("INSERT INTO portal_documents_userlink VALUES(1,%s)", (USER,))
                for table, kind in [
                    ("portal_documents_grant", "writeoff"),
                    ("commercial_invoice_grants", "sale"),
                ]:
                    db.execute(
                        f"INSERT INTO {table} VALUES(1,%s,%s,%s),(1,%s,%s,%s)",
                        (kind, SA, Jsonb(["view"]), kind, SB, Jsonb(["view", "create"])),
                    )
                for reader, kind in [(document_stores, "writeoff"), (commercial_stores, "sale")]:
                    assert set(reader(db, 1, kind)) == {SA, SB}
                db.execute(
                    "INSERT INTO portal_documents_grant VALUES"
                    "(1,'waybill',%s,%s),(1,'waybill',%s,%s)",
                    (SA, Jsonb(["edit"]), SB, Jsonb(["edit"])),
                )
                assert has_receipt_approver(db, SA) and has_receipt_approver(db, SB)
                db.execute("UPDATE chaika.web_users SET warehouse_scope_mode='selected'")
                for kind in ["waybill", "writeoff"]:
                    assert document_options(db, {"id": 1}, kind) == {
                        "stores": [],
                        "recipients": [],
                        "reasons": [],
                        "grants": [],
                    }
                assert not has_receipt_approver(db, SA)

                assert document_stores(db, 1, "writeoff") == []
                db.execute(
                    "INSERT INTO chaika.web_warehouse_access VALUES(%s,'primary',%s)", (USER, SA)
                )
                for reader, kind in [(document_stores, "writeoff"), (commercial_stores, "sale")]:
                    assert reader(db, 1, kind) == [SA]
                    assert reader(db, 1, kind, "create") == []
                for kind in ["waybill", "writeoff"]:
                    options = document_options(db, {"id": 1}, kind)
                    assert [row["id"] for row in options["stores"]] == [SA]
                    assert [row["store_id"] for row in options["grants"]] == [SA]
                assert has_receipt_approver(db, SA) and not has_receipt_approver(db, SB)
                db.execute("SET ROLE chaika_iiko_app")
                assert (
                    db.execute("SELECT store_id FROM chaika.portal_warehouse_access").fetchone()[
                        "store_id"
                    ]
                    == SA
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute("DELETE FROM chaika.web_warehouse_access")
                db.execute("RESET ROLE")
                db.execute("UPDATE chaika.web_users SET active=false")
                assert document_stores(db, 1, "writeoff") == []
                assert commercial_stores(db, 1, "sale") == []
                assert not has_receipt_approver(db, SA)
        finally:
            server.cleanup()


@pytest.mark.parametrize("sections", [["transfers"], ["sales"]])
@pytest.mark.parametrize("persisted_mode", ["selected", "all"])
def test_old_account_editor_does_not_reset_existing_warehouse_ceiling(sections, persisted_mode):
    from app.web.administration import Administration

    class SaveDatabase:
        queries = []

        def execute(self, sql, params=None):
            self.queries.append((sql, params))
            if sql.startswith("SELECT * FROM chaika.web_users"):
                return Rows(
                    [
                        {
                            "id": USER,
                            "role": "manager",
                            "revision": 1,
                            "is_portal_admin": False,
                            "warehouse_scope_mode": persisted_mode,
                        }
                    ]
                )
            return Rows([])

    db = SaveDatabase()
    admin = Administration(None)

    @contextmanager
    def write(actor):
        yield db

    admin.write = write
    admin.validate_scope = lambda db, payload: None
    payload = EditAccount(display_name="Renamed", sections=sections, revision=1)
    if persisted_mode == "all" and sections == ["sales"]:
        with pytest.raises(HTTPException) as invalid:
            admin.save_account(USER, USER, payload)
        assert invalid.value.status_code == 422
        assert not any(sql.startswith("INSERT") for sql, _ in db.queries)
        return
    admin.save_account(USER, USER, payload)
    insert = next(
        params for sql, params in db.queries if sql.startswith("INSERT INTO chaika.web_users")
    )
    assert insert[-1] == persisted_mode
    assert not any("web_warehouse_access" in sql for sql, _ in db.queries)


def test_document_moved_after_list_read_is_denied_before_detail_lines():
    repo, source = warehouse_repo()
    scope = repo.scope(USER)
    repo.resources = lambda *args, **kwargs: {"rows": [{"id": UUID(int=800)}]}

    class MovedDatabase:
        def execute(self, sql, params=None):
            assert sql.startswith("SELECT t.id FROM chaika.incoming_invoices")
            assert params == [UUID(int=800), [SA], [SA], [SA]]
            return Rows([])

    @contextmanager
    def connection(*, repeatable=False):
        assert repeatable
        yield MovedDatabase()

    repo.connection = connection
    with pytest.raises(HTTPException) as denied:
        repo.detail(scope, "invoices", UUID(int=800))
    assert denied.value.status_code == 404


def test_new_account_without_warehouse_fields_still_requires_department_scope():
    from app.web.administration import NewAccount

    with pytest.raises(ValidationError):
        NewAccount(
            display_name="New",
            sections=["sales"],
            request_id=UUID(int=888),
            email="new@example.invalid",
            password="synthetic-password",
        )
