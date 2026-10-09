"""Transactional deposits and attempts in one company's private payment schema."""

import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.tenancy.config import TenantRuntime
from app.tenancy.sql import render
from app.tenant_payments.models import PaymentPrincipal


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def serial(value):
    if isinstance(value, dict):
        return {key: serial(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serial(item) for item in value]
    if isinstance(value, (date, UUID)):
        return value.isoformat() if isinstance(value, date) else str(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else str(value)
    return value


def payment_role(runtime):
    return f"{runtime.key}_payments_runtime"


def validate_payment_connection(db, runtime):
    """Reject privileged/SET ROLE or cross-schema DSNs before handling any data."""
    role = db.execute("""
        SELECT current_user AS current_role,session_user AS session_role,
            rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls AS unsafe,
            EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid) AS member,
            has_database_privilege(current_user,current_database(),'CREATE') AS creates
        FROM pg_roles r WHERE rolname=current_user
    """).fetchone()
    expected = payment_role(runtime)
    if (
        not role
        or role["current_role"] != expected
        or role["session_role"] != expected
        or role["unsafe"]
        or role["member"]
        or role["creates"]
    ):
        raise ValueError("Payments require their dedicated restricted login")
    rows = db.execute("""
        SELECT nspname,has_schema_privilege(current_user,oid,'USAGE') AS usable,
            has_schema_privilege(current_user,oid,'CREATE') AS creates
        FROM pg_namespace WHERE nspname <> 'information_schema' AND nspname !~ '^pg_'
    """).fetchall()
    if not any(row["nspname"] == runtime.payments_schema and row["usable"] for row in rows):
        raise ValueError("Payment schema unavailable")
    if any(
        row["creates"]
        or (
            row["usable"]
            and row["nspname"] not in (runtime.payments_schema, "public", "extensions")
        )
        for row in rows
    ):
        raise ValueError("Payment role has foreign schema privileges")
    foreign = db.execute(
        """
        SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname<>%s AND n.nspname<>'information_schema' AND n.nspname !~ '^pg_'
            AND CASE WHEN c.relkind='S' THEN
                has_sequence_privilege(current_user,c.oid,'USAGE,SELECT,UPDATE')
                WHEN c.relkind IN ('r','v','m','f','p') THEN
                  has_table_privilege(current_user,c.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR has_any_column_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
                ELSE false END) AS unsafe
    """,
        (runtime.payments_schema,),
    ).fetchone()
    if foreign["unsafe"]:
        raise ValueError("Payment role has foreign data privileges")


class PaymentStore:
    def __init__(self, runtime: TenantRuntime, dsn: str, vault, *, connector=psycopg.connect):
        if runtime.mode != "tenant":
            raise ValueError("Tenant payments cannot target legacy schemas")
        self.runtime, self.dsn, self.vault, self.connector = runtime, dsn, vault, connector
        with self.connection():
            pass

    @contextmanager
    def connection(self):
        with self.connector(self.dsn, row_factory=dict_row) as db:
            validate_payment_connection(db, self.runtime)
            yield db

    def execute(self, db, template, params=()):
        try:
            return db.execute(render(template, self.runtime), params)
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "Запись или ключ повтора уже существует.") from None

    def require(self, principal, *, manage=False):
        if not isinstance(principal, PaymentPrincipal):
            raise HTTPException(403, "Проверенный субъект обязателен.")
        principal.require(self.runtime.company_id, manage=manage)

    def audit(self, db, principal, action, object_id, data=None):
        self.execute(
            db,
            "INSERT INTO {payments}.audit(actor_id,actor_kind,action,object_id,data) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                principal.actor.auth_user_id if principal else None,
                principal.actor.kind if principal else "provider_reconcile",
                action,
                object_id,
                Jsonb(data or {}),
            ),
        )

    def venue(self, db, identifier, *, lock=False):
        try:
            venue_id = UUID(str(identifier))
        except ValueError:
            row = self.execute(
                db,
                "SELECT * FROM {payments}.venues WHERE name=%s" + (" FOR UPDATE" if lock else ""),
                (str(identifier),),
            ).fetchone()
        else:
            row = self.execute(
                db,
                "SELECT * FROM {payments}.venues WHERE id=%s" + (" FOR UPDATE" if lock else ""),
                (venue_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Заведение не найдено.")
        return row

    def allowed(self, db, principal, venue_id, *, create=False):
        if principal.actor.kind == "platform_owner":
            return True
        return bool(
            self.execute(
                db,
                "SELECT 1 FROM {payments}.deposit_grants "
                "WHERE user_id=%s AND (is_all OR venue_id=%s)"
                + (" AND can_create" if create else "")
                + (" AND profile_revision=%s" if principal.profile_revision is not None else "")
                + " LIMIT 1",
                (principal.actor.auth_user_id, venue_id)
                + ((principal.profile_revision,) if principal.profile_revision is not None else ()),
            ).fetchone()
        )

    def require_venue(self, db, principal, venue_id, *, create=False):
        if not self.allowed(db, principal, venue_id, create=create):
            raise HTTPException(403, "Нет доступа к этому заведению.")

    def venues(self, principal, *, create=False):
        self.require(principal)
        with self.connection() as db:
            rows = self.execute(db, "SELECT * FROM {payments}.venues ORDER BY name").fetchall()
            return [
                {"id": str(row["id"]), "name": row["name"]}
                for row in rows
                if self.allowed(db, principal, row["id"], create=create)
                and (
                    not create
                    or (
                        row["active"]
                        and row["default_terminal_id"]
                        and self.execute(
                            db,
                            "SELECT 1 FROM {payments}.terminals WHERE id=%s "
                            "AND active AND current_version_id IS NOT NULL",
                            (row["default_terminal_id"],),
                        ).fetchone()
                    )
                )
            ]

    def management(self, principal):
        self.require(principal, manage=True)
        with self.connection() as db:
            venues = self.execute(db, "SELECT * FROM {payments}.venues ORDER BY name").fetchall()
            terminals = self.execute(
                db,
                "SELECT t.id,t.venue_id,t.name,t.active,t.revision, "
                "t.current_version_id,v.mode,v.merchant_id,(v.encrypted_key IS "
                "NOT NULL) AS key_configured "
                "FROM {payments}.terminals t LEFT JOIN {payments}.terminal_versions v "
                "ON v.id=t.current_version_id ORDER BY t.name",
            ).fetchall()
            return serial({"venues": venues, "terminals": terminals})

    def terminal_context(self, principal, venue_id, terminal_id):
        self.require(principal, manage=True)
        with self.connection() as db:
            row = self.execute(
                db,
                "SELECT v.*,t.venue_id FROM {payments}.terminal_versions v "
                "JOIN {payments}.terminals t ON v.id=t.current_version_id "
                "WHERE t.id=%s AND t.venue_id=%s",
                (terminal_id, venue_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Терминал не найден.")
            return {**row, "api_key": self.vault.decrypt(row["encrypted_key"])}

    def record_terminal_check(self, version_id, verified, *, attempt_id=None, principal=None):
        """Only provider transport results are accepted; no request DTO can supply evidence."""
        from app.tenant_payments.provider import TerminalContext

        if not isinstance(verified, TerminalContext):
            raise ValueError("Provider terminal context required")
        with self.connection() as db:
            version = self.execute(
                db, "SELECT * FROM {payments}.terminal_versions WHERE id=%s", (version_id,)
            ).fetchone()
            if not version or version["mode"] != verified.mode:
                raise HTTPException(422, "Режим терминала не подтверждён.")
            if version["merchant_id"] and version["merchant_id"] != verified.merchant_id:
                raise HTTPException(422, "Получатель платежей не соответствует настройке.")
            check_id = uuid4()
            self.execute(
                db,
                "INSERT INTO {payments}.terminal_checks "
                "(id,terminal_version_id,merchant_id,qrt_name,mode,subscription_end_date,"
                "qrt_is_b2c,requires_receipt,is_nomenclature,is_cash_link) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    check_id,
                    version_id,
                    verified.merchant_id,
                    verified.qrt_name,
                    verified.mode,
                    verified.subscription_end_date,
                    verified.qrt_is_b2c,
                    verified.requires_receipt,
                    verified.is_nomenclature,
                    verified.is_cash_link,
                ),
            )
            if attempt_id:
                row = self.execute(
                    db,
                    "SELECT terminal_version_id,state FROM {payments}.attempts "
                    "WHERE id=%s FOR UPDATE",
                    (attempt_id,),
                ).fetchone()
                if not row or row["terminal_version_id"] != version_id or row["state"] == "paid":
                    raise HTTPException(409, "Попытка оплаты изменилась.")
                self.execute(
                    db,
                    "UPDATE {payments}.attempts SET terminal_check_id=%s WHERE id=%s",
                    (check_id, attempt_id),
                )
            self.audit(db, principal, "terminal.check", version_id, {"check_id": str(check_id)})
        ready = (
            verified.subscription_end_date > datetime.now(UTC).date()
            and verified.qrt_is_b2c
            and verified.requires_receipt is False
            and verified.is_nomenclature is False
            and verified.is_cash_link is False
        )
        return {
            "id": str(check_id),
            "merchant_id": verified.merchant_id,
            "qrt_name": verified.qrt_name,
            "mode": verified.mode,
            "subscription_end_date": verified.subscription_end_date.isoformat(),
            "ready": ready,
            "requires_receipt": verified.requires_receipt,
            "is_nomenclature": verified.is_nomenclature,
            "is_cash_link": verified.is_cash_link,
        }

    def save_venue(self, principal, venue_id, payload):
        self.require(principal, manage=True)
        with self.connection() as db:
            old = self.execute(
                db, "SELECT * FROM {payments}.venues WHERE id=%s FOR UPDATE", (venue_id,)
            ).fetchone()
            if old and old["revision"] != payload.revision:
                raise HTTPException(409, "Заведение изменено. Обновите карточку.")
            if not old and payload.revision is not None:
                raise HTTPException(409, "Заведение не существует.")
            self.execute(
                db,
                "INSERT INTO {payments}.venues(id,name,active) VALUES(%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,active=EXCLUDED.active, "
                "revision={payments}.venues.revision+1",
                (venue_id, payload.name, payload.active),
            )
            self.audit(db, principal, "venue.update" if old else "venue.create", venue_id)
        return {"id": str(venue_id)}

    def save_terminal(self, principal, venue_id, terminal_id, payload):
        self.require(principal, manage=True)
        with self.connection() as db:
            venue = self.venue(db, venue_id, lock=True)
            old = self.execute(
                db, "SELECT * FROM {payments}.terminals WHERE id=%s FOR UPDATE", (terminal_id,)
            ).fetchone()
            if old and (old["venue_id"] != venue_id or old["revision"] != payload.revision):
                raise HTTPException(409, "Терминал изменён или принадлежит другому заведению.")
            if not old and (payload.revision is not None or payload.api_key is None):
                raise HTTPException(422, "Новый терминал требует ключ.")
            if (
                payload.make_default or venue["default_terminal_id"] == terminal_id
            ) and not payload.active:
                raise HTTPException(422, "Основной терминал должен быть активен.")
            previous = (
                self.execute(
                    db,
                    "SELECT * FROM {payments}.terminal_versions WHERE id=%s",
                    (old["current_version_id"],),
                ).fetchone()
                if old
                else None
            )
            encrypted = (
                self.vault.encrypt(payload.api_key.get_secret_value())
                if payload.api_key
                else previous["encrypted_key"]
            )
            revision = old["revision"] + 1 if old else 1
            version = uuid4()
            self.execute(
                db,
                "INSERT INTO {payments}.terminals(id,venue_id,name,active,revision) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name, "
                "active=EXCLUDED.active,revision=EXCLUDED.revision",
                (terminal_id, venue_id, payload.name, payload.active, revision),
            )
            self.execute(
                db,
                "INSERT INTO {payments}.terminal_versions(id,terminal_id,revision,encrypted_key, "
                "merchant_id,mode,created_by,actor_kind) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    version,
                    terminal_id,
                    revision,
                    encrypted,
                    payload.merchant_id,
                    payload.mode,
                    principal.actor.auth_user_id,
                    principal.actor.kind,
                ),
            )
            self.execute(
                db,
                "UPDATE {payments}.terminals SET current_version_id=%s WHERE id=%s",
                (version, terminal_id),
            )
            if payload.make_default or venue["default_terminal_id"] == terminal_id:
                self.execute(
                    db,
                    "UPDATE {payments}.venues SET "
                    "default_terminal_id=%s,revision=revision+1 WHERE id=%s",
                    (terminal_id, venue_id),
                )
            self.audit(
                db,
                principal,
                "terminal.update" if old else "terminal.create",
                terminal_id,
                {"version_id": str(version), "mode": payload.mode},
            )
        return {"id": str(terminal_id), "version_id": str(version), "revision": revision}

    def grant(
        self, principal, user_id, venue, *, is_all=False, can_create=False, profile_revision=None
    ):
        self.require(principal, manage=True)
        with self.connection() as db:
            venue_id = None if is_all else self.venue(db, venue)["id"]
            saved = self.execute(
                db,
                "INSERT INTO {payments}.deposit_grants "
                "(id,user_id,venue_id,is_all,can_create,profile_revision) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,venue_id) DO UPDATE "
                "SET is_all=EXCLUDED.is_all,can_create=COALESCE(%s,"
                "{payments}.deposit_grants.can_create),profile_revision=EXCLUDED.profile_revision "
                "RETURNING can_create",
                (
                    uuid4(),
                    user_id,
                    venue_id,
                    is_all,
                    can_create is True,
                    profile_revision,
                    can_create,
                ),
            ).fetchone()
            can_create = saved["can_create"]
            self.audit(
                db,
                principal,
                "deposit_access.grant",
                user_id,
                {
                    "venue_id": str(venue_id) if venue_id else None,
                    "is_all": is_all,
                    "can_create": can_create,
                },
            )
        return {
            "user_id": str(user_id),
            "venue_id": str(venue_id) if venue_id else None,
            "venue": str(venue),
            "is_all": is_all,
            "can_create": can_create,
        }

    def grants(self, principal):
        self.require(principal, manage=True)
        with self.connection() as db:
            return serial(
                self.execute(
                    db,
                    "SELECT g.user_id,g.venue_id,g.is_all,g.can_create,g.profile_revision, "
                    "COALESCE(v.name,'Все заведения') AS venue FROM {payments}.deposit_grants g "
                    "LEFT JOIN {payments}.venues v ON v.id=g.venue_id ORDER BY g.user_id,v.name",
                ).fetchall()
            )

    def revoke(self, principal, user_id, venue, *, is_all=False):
        self.require(principal, manage=True)
        with self.connection() as db:
            venue_id = None if is_all else self.venue(db, venue)["id"]
            self.execute(
                db,
                "DELETE FROM {payments}.deposit_grants WHERE user_id=%s AND "
                "venue_id IS NOT DISTINCT FROM %s",
                (user_id, venue_id),
            )
            self.audit(db, principal, "deposit_access.revoke", user_id)

    def _deposit(self, db, deposit_id, *, lock=False):
        row = self.execute(
            db,
            "SELECT d.*,v.name AS restaurant FROM {payments}.deposits d "
            "JOIN {payments}.venues v ON v.id=d.venue_id WHERE d.id=%s"
            + (" FOR UPDATE OF d" if lock else ""),
            (deposit_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Депозит не найден.")
        return row

    def public_deposit(self, row, *, staff=False):
        result = {
            key: row[key]
            for key in (
                "id",
                "venue_id",
                "restaurant",
                "amount_minor",
                "currency",
                "status",
                "paid_at",
                "reservation_date",
                "created_at",
                "updated_at",
                "revision",
            )
        }
        result["amount"] = row["amount_minor"] // 100
        if staff:
            result.update({key: row[key] for key in ("customer_name", "phone", "notes")})
            token = self.vault.decrypt(row["encrypted_guest_token"])
            result["guest_url"] = (
                f"{self.runtime.frontend_origin}/deposit/{row['id']}?" + urlencode({"token": token})
            )
        return serial(result)

    def create(self, principal, payload):
        self.require(principal)
        fingerprint = digest(
            json.dumps(
                payload.model_dump(mode="json", exclude={"request_id"}),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        with self.connection() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,72682))",
                (f"{self.runtime.key}:deposit:{payload.request_id}",),
            )
            venue = self.venue(db, payload.restaurant, lock=True)
            self.require_venue(db, principal, venue["id"], create=True)
            previous = self.execute(
                db,
                "SELECT id,created_by,actor_kind,fingerprint FROM "
                "{payments}.deposits WHERE request_id=%s",
                (payload.request_id,),
            ).fetchone()
            if previous:
                if (
                    previous["created_by"] != principal.actor.auth_user_id
                    or previous["actor_kind"] != principal.actor.kind
                    or previous["fingerprint"] != fingerprint
                ):
                    raise HTTPException(409, "Ключ повтора принадлежит другому запросу.")
                return self.public_deposit(self._deposit(db, previous["id"]), staff=True)
            if not venue["active"] or not venue["default_terminal_id"]:
                raise HTTPException(422, "Заведение не готово к созданию депозитов.")
            terminal = self.execute(
                db,
                "SELECT 1 FROM {payments}.terminals WHERE id=%s AND active AND "
                "current_version_id IS NOT NULL",
                (venue["default_terminal_id"],),
            ).fetchone()
            if not terminal:
                raise HTTPException(422, "Основной терминал недоступен.")
            deposit_id, guest = uuid4(), secrets.token_urlsafe(32)
            booking_day = (
                payload.reservation_date.astimezone(ZoneInfo(self.runtime.timezone)).date()
                if payload.reservation_date
                else None
            )
            self.execute(
                db,
                "INSERT INTO "
                "{payments}.deposits(id,request_id,fingerprint,venue_id,customer_name,phone, "
                "amount_minor,reservation_date,reservation_day,notes,created_by,actor_kind,"
                "guest_token_hash,encrypted_guest_token) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    deposit_id,
                    payload.request_id,
                    fingerprint,
                    venue["id"],
                    payload.customer_name,
                    payload.phone,
                    int(payload.amount) * 100,
                    payload.reservation_date,
                    booking_day,
                    payload.notes,
                    principal.actor.auth_user_id,
                    principal.actor.kind,
                    digest(guest),
                    self.vault.encrypt(guest),
                ),
            )
            self.audit(db, principal, "deposit.create", deposit_id, {"venue_id": str(venue["id"])})
            return self.public_deposit(self._deposit(db, deposit_id), staff=True)

    def get(self, principal, deposit_id):
        self.require(principal)
        with self.connection() as db:
            row = self._deposit(db, deposit_id)
            self.require_venue(db, principal, row["venue_id"])
            return self.public_deposit(row, staff=True)

    def guest(self, deposit_id, token):
        with self.connection() as db:
            row = self._deposit(db, deposit_id)
            self.guest_authorize(row, token)
            result = self.public_deposit(row)
            attempt = self.execute(
                db,
                "SELECT a.state,a.payment_url,a.qr_image,a.diagnostic,a.valid_until,v.mode "
                "FROM {payments}.attempts a JOIN {payments}.terminal_versions v "
                "ON v.id=a.terminal_version_id WHERE a.deposit_id=%s "
                "ORDER BY a.created_at DESC LIMIT 1",
                (deposit_id,),
            ).fetchone()
            result["payment"] = serial(attempt) if attempt else None
            return result

    @staticmethod
    def guest_authorize(row, token):
        if (
            not isinstance(token, str)
            or len(token) > 128
            or not hmac.compare_digest(row["guest_token_hash"], digest(token))
        ):
            raise HTTPException(404, "Ссылка не найдена.")

    def listing(self, principal, filters, *, export=False):
        self.require(principal)
        with self.connection() as db:
            clauses, values = [], []
            if principal.actor.kind != "platform_owner":
                clauses.append(
                    "EXISTS(SELECT 1 FROM {payments}.deposit_grants g WHERE "
                    "g.user_id=%s AND (g.is_all OR g.venue_id=d.venue_id)"
                    + (
                        " AND g.profile_revision=%s"
                        if principal.profile_revision is not None
                        else ""
                    )
                    + ")"
                )
                values.append(principal.actor.auth_user_id)
                if principal.profile_revision is not None:
                    values.append(principal.profile_revision)
            if filters.restaurant:
                clauses.append("d.venue_id=%s")
                values.append(self.venue(db, filters.restaurant)["id"])
            for field, operator, value in (
                ("status", "=", filters.status_filter),
                (
                    "amount_minor",
                    ">=",
                    None if filters.min_amount is None else Decimal(str(filters.min_amount)) * 100,
                ),
                (
                    "amount_minor",
                    "<=",
                    None if filters.max_amount is None else Decimal(str(filters.max_amount)) * 100,
                ),
                ("created_at", ">=", filters.date_from),
                ("created_at", "<=", filters.date_to),
                ("reservation_day", ">=", filters.reservation_from),
                ("reservation_day", "<=", filters.reservation_to),
            ):
                if value is not None:
                    clauses.append(f"d.{field}{operator}%s")
                    values.append(value)
            if filters.query:
                # Escape LIKE metacharacters; query is a literal substring, never SQL.
                escaped = (
                    filters.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                clauses.append(
                    "(d.customer_name ILIKE %s OR d.phone ILIKE %s OR v.name "
                    "ILIKE %s OR COALESCE(d.notes,'') ILIKE %s)"
                )
                values.extend([f"%{escaped}%"] * 4)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            source = " FROM {payments}.deposits d JOIN {payments}.venues v ON v.id=d.venue_id"
            total = self.execute(db, "SELECT count(*) AS n" + source + where, values).fetchone()[
                "n"
            ]
            columns = {
                "restaurant": "v.name",
                "amount": "d.amount_minor",
                "reservation_date": "d.reservation_date",
                **{
                    key: f"d.{key}"
                    for key in ("created_at", "paid_at", "customer_name", "phone", "status")
                },
            }
            order = (
                columns[filters.sort_by]
                + (" ASC" if filters.sort_dir == "asc" else " DESC")
                + " NULLS LAST,d.id"
            )
            limit = 1000 if export else filters.page_size
            offset = 0 if export else (filters.page - 1) * filters.page_size
            rows = self.execute(
                db,
                "SELECT d.*,v.name AS restaurant"
                + source
                + where
                + f" ORDER BY {order} LIMIT %s OFFSET %s",
                (*values, limit, offset),
            ).fetchall()
            return {
                "items": [self.public_deposit(row, staff=True) for row in rows],
                "total": total,
                "page": 1 if export else filters.page,
                "page_size": limit,
            }

    def edit(self, principal, deposit_id, payload, revision):
        """No financial rewrite once an attempt was reserved, including unknown POSTs."""
        self.require(principal)
        with self.connection() as db:
            row = self._deposit(db, deposit_id, lock=True)
            self.require_venue(db, principal, row["venue_id"], create=True)
            if (
                row["revision"] != revision
                or self.execute(
                    db,
                    "SELECT 1 FROM {payments}.attempts WHERE deposit_id=%s LIMIT 1",
                    (deposit_id,),
                ).fetchone()
            ):
                raise HTTPException(409, "Депозит изменён или уже передан в оплату.")
            venue = self.venue(db, payload.restaurant)
            self.require_venue(db, principal, venue["id"], create=True)
            if not venue["active"]:
                raise HTTPException(422, "Заведение отключено.")
            booking = (
                payload.reservation_date.astimezone(ZoneInfo(self.runtime.timezone)).date()
                if payload.reservation_date
                else None
            )
            self.execute(
                db,
                "UPDATE {payments}.deposits SET "
                "venue_id=%s,customer_name=%s,phone=%s,amount_minor=%s, "
                "reservation_date=%s,reservation_day=%s,notes=%s,revision=revision+1,"
                "updated_at=now() WHERE id=%s",
                (
                    venue["id"],
                    payload.customer_name,
                    payload.phone,
                    int(payload.amount) * 100,
                    payload.reservation_date,
                    booking,
                    payload.notes,
                    deposit_id,
                ),
            )
            self.audit(db, principal, "deposit.edit", deposit_id)
            return self.public_deposit(self._deposit(db, deposit_id), staff=True)

    def reserve(self, deposit_id, token, request_id):
        """Commit the intent before returning a credential for the external POST."""
        with self.connection() as db:
            deposit = self._deposit(db, deposit_id, lock=True)
            self.guest_authorize(deposit, token)
            old = self.execute(
                db, "SELECT * FROM {payments}.attempts WHERE request_id=%s", (request_id,)
            ).fetchone()
            if old:
                if old["deposit_id"] != deposit_id:
                    raise HTTPException(409, "Ключ повтора занят.")
                return serial(old), None
            if deposit["status"] == "paid":
                return {"state": "paid", "deposit_id": str(deposit_id)}, None
            old = self.execute(
                db,
                "SELECT * FROM {payments}.attempts WHERE deposit_id=%s AND "
                "state IN ('creating','unknown','pending') "
                "ORDER BY created_at DESC LIMIT 1",
                (deposit_id,),
            ).fetchone()
            if old:
                # New request IDs cannot escape an uncertain creation or pending operation.
                return serial(old), None
            venue = self.venue(db, deposit["venue_id"])
            terminal = self.execute(
                db,
                "SELECT t.*,v.encrypted_key,v.mode,v.merchant_id,v.currency "
                "FROM {payments}.terminals t JOIN {payments}.terminal_versions "
                "v ON v.id=t.current_version_id "
                "WHERE t.id=%s AND t.venue_id=%s AND t.active",
                (venue["default_terminal_id"], venue["id"]),
            ).fetchone()
            if not venue["active"] or not terminal:
                raise HTTPException(422, "Оплата временно недоступна.")
            attempt_id, callback = uuid4(), secrets.token_urlsafe(32)
            row = self.execute(
                db,
                "INSERT INTO "
                "{payments}.attempts(id,deposit_id,request_id,terminal_id,terminal_version_id, "
                "amount_minor,currency,state,callback_token_hash,next_check_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'creating',%s,"
                "now()+interval '30 seconds') RETURNING *",
                (
                    attempt_id,
                    deposit_id,
                    request_id,
                    terminal["id"],
                    terminal["current_version_id"],
                    deposit["amount_minor"],
                    deposit["currency"],
                    digest(callback),
                ),
            ).fetchone()
            self.audit(
                db,
                None,
                "payment.reserve",
                attempt_id,
                {
                    "deposit_id": str(deposit_id),
                    "terminal_version_id": str(terminal["current_version_id"]),
                },
            )
            self.execute(
                db,
                "UPDATE {payments}.deposits SET status='pending',updated_at=now() "
                "WHERE id=%s AND status='failed'",
                (deposit_id,),
            )
            return serial(row), {
                "api_key": self.vault.decrypt(terminal["encrypted_key"]),
                "mode": terminal["mode"],
                "terminal_version_id": terminal["current_version_id"],
                "amount_minor": deposit["amount_minor"],
                "currency": deposit["currency"],
                "callback": callback,
                "redirect_url": f"{self.runtime.frontend_origin}/deposit/{deposit_id}?"
                + urlencode({"token": token}),
            }

    def record_creation(self, attempt_id, created=None, *, rejected=False):
        with self.connection() as db:
            row = self.execute(
                db, "SELECT * FROM {payments}.attempts WHERE id=%s FOR UPDATE", (attempt_id,)
            ).fetchone()
            if created and (not row["operation_id"] or row["operation_id"] == created.operation_id):
                from app.tenant_payments.provider import _qr_currency_evidence

                evidence = (
                    _qr_currency_evidence(created.qr_payload, row["amount_minor"])
                    if created.currency_evidence == "sbp_qr_payload"
                    else None
                )
                self.execute(
                    db,
                    "UPDATE {payments}.attempts SET creation_confirmed=true,"
                    "creation_currency=%s,provider_amount_minor=%s,provider_qr_payload=%s "
                    "WHERE id=%s",
                    (
                        evidence[0] if evidence else None,
                        evidence[1] if evidence else None,
                        evidence[2] if evidence else None,
                        attempt_id,
                    ),
                )
            if row["state"] in ("paid", "failed", "expired"):
                # A callback may finish before POST returns. Preserve its verified result.
                if created and (
                    not row["operation_id"] or row["operation_id"] == created.operation_id
                ):
                    self.execute(
                        db,
                        "UPDATE {payments}.attempts SET operation_id=%s,payment_url=%s,"
                        "qr_image=%s,valid_until=%s,updated_at=now() WHERE id=%s",
                        (
                            created.operation_id,
                            created.payment_url,
                            created.qr_image,
                            created.valid_until,
                            attempt_id,
                        ),
                    )
                return
            if created is None:
                self.execute(
                    db,
                    "UPDATE {payments}.attempts SET "
                    "state=%s,diagnostic=%s,updated_at=now() WHERE id=%s",
                    (
                        "failed" if rejected and not row["operation_id"] else "unknown",
                        "creation_rejected" if rejected else "creation_unknown",
                        attempt_id,
                    ),
                )
            else:
                if row["operation_id"] and row["operation_id"] != created.operation_id:
                    self.execute(
                        db,
                        "UPDATE {payments}.attempts SET "
                        "state='unknown',diagnostic='operation_mismatch' WHERE "
                        "id=%s",
                        (attempt_id,),
                    )
                    return
                self.execute(
                    db,
                    "UPDATE {payments}.attempts SET "
                    "state='pending',operation_id=%s,payment_url=%s, "
                    "qr_image=%s,valid_until=%s,diagnostic=NULL,next_check_at=now(),"
                    "updated_at=now() WHERE id=%s",
                    (
                        created.operation_id,
                        created.payment_url,
                        created.qr_image,
                        created.valid_until,
                        attempt_id,
                    ),
                )
            self.audit(db, None, "payment.creation_result", attempt_id)

    def check_context(self, attempt_id):
        with self.connection() as db:
            row = self.execute(
                db,
                "SELECT a.*,v.encrypted_key,v.mode,v.merchant_id FROM {payments}.attempts a "
                "JOIN {payments}.terminal_versions v ON v.id=a.terminal_version_id WHERE a.id=%s",
                (attempt_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Попытка оплаты не найдена.")
            return {**row, "api_key": self.vault.decrypt(row["encrypted_key"])}

    def record_check(self, attempt_id, result=None, *, diagnostic=None):
        with self.connection() as db:
            row = self.execute(
                db,
                "SELECT a.*,v.merchant_id,c.merchant_id AS validated_merchant "
                "FROM {payments}.attempts a JOIN "
                "{payments}.terminal_versions v "
                "ON v.id=a.terminal_version_id LEFT JOIN {payments}.terminal_checks c "
                "ON c.id=a.terminal_check_id WHERE a.id=%s FOR UPDATE OF a",
                (attempt_id,),
            ).fetchone()
            if row["state"] == "paid":
                return "paid"
            state = row["state"]
            if state == "creating" and result is None and diagnostic == "creation_unknown":
                state = "unknown"
            if result is not None:
                # A provider-returned expanded SBP QR is prior operation evidence, never an
                # invoice default. An explicit current currency always takes precedence.
                currency = result.currency
                if (
                    currency is None
                    and row["creation_confirmed"]
                    and (row["provider_amount_minor"] == row["amount_minor"])
                ):
                    currency = row["creation_currency"]
                merchant = result.merchant
                if merchant is None and row["creation_confirmed"]:
                    merchant = row["validated_merchant"]
                expected_merchant = row["merchant_id"] or row["validated_merchant"]
                if not row["validated_merchant"]:
                    diagnostic = "terminal_unverified"
                elif result.operation_id != row["operation_id"]:
                    diagnostic = "operation_mismatch"
                elif result.amount_minor != row["amount_minor"]:
                    diagnostic = "amount_mismatch"
                elif currency != row["currency"]:
                    diagnostic = "currency_unverified" if currency is None else "currency_mismatch"
                elif expected_merchant and merchant != expected_merchant:
                    diagnostic = "merchant_unverified" if merchant is None else "merchant_mismatch"
                elif result.status in ("pending", "paid", "failed", "expired"):
                    # Terminal attempts cannot reopen on an older pending observation.
                    if state not in ("failed", "expired") or result.status == "paid":
                        state = result.status
                    diagnostic = None
                else:
                    diagnostic = "status_unknown"
            if state == "paid":
                self.execute(
                    db,
                    "UPDATE {payments}.deposits SET "
                    "status='paid',paid_at=COALESCE(paid_at,%s,now()), "
                    "updated_at=now(),revision=revision+1 WHERE id=%s AND status<>'paid'",
                    (result.paid_at, row["deposit_id"]),
                )
                self.audit(
                    db, None, "payment.paid", attempt_id, {"deposit_id": str(row["deposit_id"])}
                )
            elif state in ("failed", "expired"):
                self.execute(
                    db,
                    "UPDATE {payments}.deposits SET status='failed',updated_at=now() "
                    "WHERE id=%s AND status<>'paid' AND NOT EXISTS("
                    "SELECT 1 FROM {payments}.attempts WHERE deposit_id=%s "
                    "AND id<>%s AND created_at>%s)",
                    (row["deposit_id"], row["deposit_id"], attempt_id, row["created_at"]),
                )
            # A provider-declared failed/expired operation is the only safe route to a new attempt.
            self.execute(
                db,
                "UPDATE {payments}.attempts SET state=%s,diagnostic=%s,checked_at=now(), "
                "check_count=check_count+1,next_check_at=now()+interval '60 "
                "seconds',updated_at=now() WHERE id=%s",
                (state, diagnostic, attempt_id),
            )
            return state

    def callback(self, attempt_id, token, operation_id):
        with self.connection() as db:
            row = self.execute(
                db, "SELECT * FROM {payments}.attempts WHERE id=%s FOR UPDATE", (attempt_id,)
            ).fetchone()
            if (
                not row
                or not isinstance(token, str)
                or len(token) > 128
                or not hmac.compare_digest(row["callback_token_hash"], digest(token))
            ):
                raise HTTPException(404, "Уведомление не найдено.")
            if row["operation_id"] and row["operation_id"] != operation_id:
                raise HTTPException(409, "Чужая операция.")
            if row["operation_id"] is None:
                self.execute(
                    db,
                    "UPDATE {payments}.attempts SET "
                    "operation_id=%s,state='unknown',next_check_at=now() WHERE "
                    "id=%s",
                    (operation_id, attempt_id),
                )
            receipt = self.execute(
                db,
                "INSERT INTO {payments}.webhook_receipts(attempt_id,fingerprint) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id",
                (attempt_id, digest(str(operation_id))),
            ).fetchone()
            if receipt and row["state"] != "paid":
                self.execute(
                    db,
                    "UPDATE {payments}.attempts SET next_check_at=now() WHERE id=%s",
                    (attempt_id,),
                )
        # Callback is only a hint. No caller-controlled status is ever stored.
        return self.check_context(attempt_id)

    def lease_check(self, attempt_id):
        with self.connection() as db:
            row = self.execute(
                db,
                "UPDATE {payments}.attempts SET next_check_at=now()+interval '60 seconds' "
                "WHERE id=%s AND state IN ('creating','unknown','pending','failed','expired') AND "
                "next_check_at<=now() RETURNING id",
                (attempt_id,),
            ).fetchone()
            return bool(row)

    def latest_attempt(self, deposit_id):
        with self.connection() as db:
            row = self.execute(
                db,
                "SELECT id FROM {payments}.attempts WHERE deposit_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (deposit_id,),
            ).fetchone()
            return row["id"] if row else None

    def due(self, *, limit=20):
        with self.connection() as db:
            # Reserve a bounded check lease; works across restarts and concurrent workers.
            rows = self.execute(
                db,
                "SELECT id FROM {payments}.attempts WHERE state IN "
                "('creating','unknown','pending') "
                "AND next_check_at<=now() ORDER BY next_check_at FOR UPDATE SKIP LOCKED LIMIT %s",
                (limit,),
            ).fetchall()
            for row in rows:
                self.execute(
                    db,
                    "UPDATE {payments}.attempts SET "
                    "next_check_at=now()+interval '60 seconds' WHERE id=%s",
                    (row["id"],),
                )
            return [row["id"] for row in rows]
