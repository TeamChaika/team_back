"""Venue expenses, their accrual dates and an append-only history of corrections."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.web.password_policy import require_personal_password
from app.web.permissions import finance_level

Category = Literal["rent", "payroll", "utilities", "repairs", "marketing", "services", "other"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)


class ExpenseFields(Input):
    expense_date: date
    paid_date: date | None = None
    category: Category
    amount: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
    payment_method: Literal["cash", "bank"]
    comment: str = Field(default="", max_length=500)

    @field_validator("paid_date")
    @classmethod
    def actual_payment_date(cls, value):
        if value and value > datetime.now(ZoneInfo("Europe/Moscow")).date():
            raise ValueError("Дата оплаты не может быть в будущем.")
        return value


class ExpenseCreate(ExpenseFields):
    request_id: UUID
    department_id: UUID


class ExpenseEdit(ExpenseFields):
    revision: int = Field(ge=1)


class ExpenseCancel(Input):
    revision: int = Field(ge=1)


@dataclass(frozen=True)
class Access:
    actor: UUID
    level: str
    venues: dict

    def require_writable(self, venue):
        if not self.venues.get(venue, {}).get("writable"):
            raise HTTPException(403, "Ввод расходов недоступен для этого заведения.")

    @property
    def visibility(self):
        condition = "e.department_id=ANY(%s::uuid[])"
        params = [list(self.venues)]
        if self.level == "manager":
            condition += " AND e.author_id=%s"
            params.append(self.actor)
        return condition, params


# The invoice and sales ledgers are deliberately separate from this manual register.
FIELDS = ",".join(ExpenseFields.model_fields)
VISIBLE = (
    "SELECT e.id,e.department_id,e.author_id,u.display_name AS author,e.expense_date,"
    "e.paid_date,e.category,e.amount,e.payment_method,e.comment,e.revision,e.cancelled "
    "FROM chaika.finance_expenses e JOIN chaika.web_users u ON u.id=e.author_id WHERE "
)


def state(row):
    result = {key: row[key] for key in ExpenseFields.model_fields}
    result["expense_date"] = result["expense_date"].isoformat()
    if result["paid_date"] is not None:
        result["paid_date"] = result["paid_date"].isoformat()
    result["amount"] = format(result["amount"], ".2f")
    result["cancelled"] = row["cancelled"]
    return result


def public_row(row, access):
    return {
        **state(row),
        "id": str(row["id"]),
        "department_id": str(row["department_id"]),
        "department": access.venues[row["department_id"]]["name"],
        "author": row["author"],
        "revision": row["revision"],
        "can_edit": bool(
            not row["cancelled"]
            and access.venues[row["department_id"]]["writable"]
            and (access.level != "manager" or row["paid_date"] is None)
        ),
    }


def _access(db, scope, *, writing):
    # Account edits lock the same user before replacing their department grants.
    profile = db.execute(
        "SELECT id,role,sections,all_departments,warehouse_scope_mode,password_change_required "
        "FROM chaika.web_users WHERE id=%s AND active" + (" FOR SHARE" if writing else ""),
        (scope.user["id"],),
    ).fetchone()
    if not profile:
        raise HTTPException(403, "Доступ к журналу не назначен.")
    require_personal_password(profile)
    level = finance_level(profile)
    if not level or profile["warehouse_scope_mode"] != "all":
        raise HTTPException(403, "Нет доступа к финансам заведений.")
    if writing:
        # The settings writer takes the exclusive lock before archiving a venue.
        db.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
            ("chaika:operation-settings",),
        )
    venues = db.execute(
        "SELECT n.id,n.name,n.present_in_latest AND NOT (coalesce((SELECT settings "
        "#> '{analytics,archived_department_ids}' FROM chaika.operation_settings WHERE singleton),"
        "'[]'::jsonb) ? n.id::text) AS writable FROM chaika.corporate_nodes n "
        "WHERE n.source_id='primary' AND n.type='DEPARTMENT' AND n.id=ANY(%s::uuid[]) "
        "AND (%s OR EXISTS (SELECT FROM chaika.web_department_access a WHERE "
        "a.source_id=n.source_id AND a.department_id=n.id AND a.user_id=%s))"
        + (" ORDER BY n.id FOR SHARE OF n" if writing else ""),
        (list(scope.ids), profile["all_departments"], profile["id"]),
    ).fetchall()
    return Access(profile["id"], level, {r["id"]: r for r in venues})


def _read_one(db, access, expense_id, *, lock=False):
    condition, params = access.visibility
    row = db.execute(
        VISIBLE + condition + " AND e.id=%s" + (" FOR UPDATE OF e" if lock else ""),
        [*params, expense_id],
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Расход не найден.")
    return row


def _record(db, access, action, row, before=None):
    db.execute(
        "INSERT INTO chaika.finance_expense_history "
        "(expense_id,revision,actor_id,action,before_state,after_state) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            row["id"],
            row["revision"],
            access.actor,
            action,
            Jsonb(state(before)) if before else None,
            Jsonb(state(row)),
        ),
    )


class FinanceLedger:
    def __init__(self, repo):
        self.repo = repo

    @contextmanager
    def _write(self, scope):
        self.repo._pool.open()
        with self.repo._pool.connection() as db, db.transaction():
            db.execute("SET LOCAL statement_timeout='15000ms'")
            yield db, _access(db, scope, writing=True)

    def list(self, scope, start, end, *, limit, offset):
        with self.repo.connection(repeatable=True) as db:
            access = _access(db, scope, writing=False)
            condition, params = access.visibility
            condition += " AND e.expense_date BETWEEN %s AND %s"
            params += [start, end]
            total = db.execute(
                "SELECT count(*) AS total FROM chaika.finance_expenses e WHERE " + condition, params
            ).fetchone()["total"]
            rows = db.execute(
                VISIBLE + condition + " ORDER BY e.expense_date DESC,e.created_at DESC,e.id "
                "LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
            return {"rows": [public_row(r, access) for r in rows], "total": total}

    def totals(self, scope, start, end):
        with self.repo.connection(repeatable=True) as db:
            access = _access(db, scope, writing=False)
            if access.level != "owner":
                raise HTTPException(403, "Финансовая сводка доступна только владельцу.")
            if set(scope.ids) != set(access.venues):
                raise HTTPException(403, "Изменился доступ к выбранным заведениям. Обновите отчёт.")
            condition, params = access.visibility
            groups = db.execute(
                "SELECT category,payment_method,sum(amount) AS amount "
                "FROM chaika.finance_expenses e WHERE "
                + condition
                + " AND NOT cancelled AND expense_date BETWEEN %s AND %s "
                "GROUP BY category,payment_method ORDER BY category",
                [*params, start, end],
            ).fetchall()
        amounts = {"cash": Decimal(0), "bank": Decimal(0)}
        categories = {}
        for row in groups:
            amounts[row["payment_method"]] += row["amount"]
            key = row["category"]
            categories[key] = categories.get(key, Decimal(0)) + row["amount"]
        return {
            "expenses": format(sum(amounts.values()), ".2f"),
            **{key: format(value, ".2f") for key, value in amounts.items()},
            "by_category": [
                {"category": key, "amount": format(value, ".2f")}
                for key, value in categories.items()
            ],
        }

    def create(self, scope, payload):
        with self._write(scope) as (db, access):
            venue = access.venues.get(payload.department_id)
            if venue is None:
                raise HTTPException(403, "Нет доступа к этому заведению.")
            added = db.execute(
                "INSERT INTO chaika.finance_expenses (department_id,author_id,request_id,"
                + FIELDS
                + ") SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s WHERE %s "
                "ON CONFLICT (author_id,request_id) DO NOTHING RETURNING id",
                (
                    payload.department_id,
                    access.actor,
                    payload.request_id,
                    *(getattr(payload, field) for field in ExpenseFields.model_fields),
                    venue["writable"],
                ),
            ).fetchone()
            if added:
                row = _read_one(db, access, added["id"])
                _record(db, access, "create", row)
            else:
                original = db.execute(
                    "SELECT e.id,e.department_id,h.after_state FROM chaika.finance_expenses e "
                    "JOIN chaika.finance_expense_history h ON h.expense_id=e.id AND h.revision=1 "
                    "WHERE e.author_id=%s AND e.request_id=%s",
                    (access.actor, payload.request_id),
                ).fetchone()
                submitted = state({**payload.model_dump(), "cancelled": False})
                if original is None:
                    raise HTTPException(403, "Ввод расходов недоступен для этого заведения.")
                if (
                    original["department_id"] != payload.department_id
                    or original["after_state"] != submitted
                ):
                    raise HTTPException(409, "Номер запроса уже использован для другого расхода.")
                row = _read_one(db, access, original["id"])
            return public_row(row, access)

    def update(self, scope, expense_id, payload):
        return self._change(scope, expense_id, payload, "update")

    def cancel(self, scope, expense_id, payload):
        return self._change(scope, expense_id, payload, "cancel")

    def _change(self, scope, expense_id, payload, action):
        with self._write(scope) as (db, access):
            before = _read_one(db, access, expense_id, lock=True)
            access.require_writable(before["department_id"])
            if before["paid_date"] and access.level == "manager":
                raise HTTPException(
                    403, "Оплаченный расход может исправить только бухгалтерия или владелец."
                )
            if before["cancelled"] or before["revision"] != payload.revision:
                raise HTTPException(409, "Расход уже изменён. Обновите журнал.")
            if action == "cancel":
                db.execute(
                    "UPDATE chaika.finance_expenses SET cancelled=true,revision=revision+1 "
                    "WHERE id=%s",
                    (expense_id,),
                )
            else:
                db.execute(
                    "UPDATE chaika.finance_expenses SET (" + FIELDS + ")="
                    "(%s,%s,%s,%s,%s,%s), revision=revision+1 WHERE id=%s",
                    (
                        *(getattr(payload, field) for field in ExpenseFields.model_fields),
                        expense_id,
                    ),
                )
            row = _read_one(db, access, expense_id)
            _record(db, access, action, row, before)
            return public_row(row, access)

    def history(self, scope, expense_id):
        with self.repo.connection(repeatable=True) as db:
            access = _access(db, scope, writing=False)
            _read_one(db, access, expense_id)
            rows = db.execute(
                "SELECT h.revision,h.action,u.display_name AS actor,h.changed_at,"
                "h.before_state AS before,h.after_state AS after "
                "FROM chaika.finance_expense_history h JOIN chaika.web_users u ON u.id=h.actor_id "
                "WHERE h.expense_id=%s "
                "ORDER BY h.revision DESC",
                (expense_id,),
            ).fetchall()
            return {"rows": [{**r, "changed_at": r["changed_at"].isoformat()} for r in rows]}
