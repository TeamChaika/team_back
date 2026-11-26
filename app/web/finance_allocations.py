"""Atomic shared-expense groups backed by ordinary accrual ledger entries."""

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.web.finance_ledger import FIELDS, ExpenseFields, FinanceLedger, _access, _record, state


def _require_accounting(access):
    if access.level not in {"accountant", "owner"}:
        raise HTTPException(403, "Распределение расходов доступно бухгалтерии и владельцу.")


def _require_parts(access, parts):
    for part in parts:
        venue = UUID(str(part["department_id"]))
        if venue not in access.venues:
            raise HTTPException(403, "Нет доступа ко всем заведениям распределения.")
        access.require_writable(venue)


def _request(payload):
    result = payload.model_dump(mode="json", exclude={"request_id", "revision"})
    # Decimal spelling and participant order have no semantic significance.
    result["amount"] = format(payload.amount, ".2f")
    result["parts"] = sorted(result["parts"], key=lambda part: part["department_id"])
    for part in result["parts"]:
        if part.get("weight") is not None:
            part["weight"] = str(Decimal(part["weight"]).normalize())
        part.pop("amount", None)  # Preview confirmation is not the expense's identity.
    return result


def _calculated(payload, calculate, db):
    if payload.method == "pending":
        return [{"department_id": str(part.department_id)} for part in payload.parts]
    parts = calculate(payload, db)
    wanted = {part.department_id for part in payload.parts}
    actual = [UUID(str(part["department_id"])) for part in parts]
    if len(actual) != len(set(actual)) or set(actual) != wanted:
        raise ValueError("Allocation calculator returned different participants")
    normalized = []
    for part in parts:
        amount, weight = Decimal(part["amount"]), Decimal(part["weight"])
        if (not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal(".01"))
                or not weight.is_finite() or weight < 0):
            raise ValueError("Allocation calculator returned invalid amounts or weights")
        normalized.append({"department_id": str(part["department_id"]),
                           "weight": str(weight), "amount": format(amount, ".2f")})
    if sum(Decimal(part["amount"]) for part in normalized) != payload.amount:
        raise ValueError("Allocation calculator did not preserve total")
    return sorted(normalized, key=lambda part: part["department_id"])


def _snapshot(row):
    return {**state(row), "method": row["method"], "parts": row["parts"]}


def _read(db, access, allocation_id, *, lock=False):
    _require_accounting(access)
    row = db.execute(
        "SELECT * FROM chaika.finance_allocations WHERE id=%s "
        "AND access_department_ids <@ %s::uuid[]" + (" FOR UPDATE" if lock else ""),
        (allocation_id, list(access.venues)),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Распределение не найдено.")
    return row


def _public(row, access):
    return {
        **_snapshot(row), "id": str(row["id"]), "revision": row["revision"],
        "parts": [{**part, "department": access.venues[UUID(part["department_id"])]["name"]}
                  for part in row["parts"]],
        "can_edit": not row["cancelled"] and (row["method"] == "pending" or all(
            access.venues[UUID(part["department_id"])]["writable"] for part in row["parts"]
        )),
    }


def _history(db, access, action, row, before=None):
    db.execute(
        "INSERT INTO chaika.finance_allocation_history "
        "(allocation_id,revision,actor_id,action,before_state,after_state) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (row["id"], row["revision"], access.actor, action,
         Jsonb(_snapshot(before)) if before else None, Jsonb(_snapshot(row))),
    )


def _sync_expenses(db, access, row):
    """Keep child identity and append a venue-only history, never group snapshots."""
    existing = db.execute(
        "SELECT * FROM chaika.finance_expenses WHERE allocation_id=%s ORDER BY department_id "
        "FOR UPDATE", (row["id"],),
    ).fetchall()
    by_venue = {expense["department_id"]: expense for expense in existing}
    active = {UUID(part["department_id"]): Decimal(part["amount"])
              for part in row["parts"] if Decimal(part.get("amount", "0")) > 0}
    for venue in sorted(set(active) | set(by_venue), key=str):
        before = by_venue.get(venue)
        cancelled = row["cancelled"] or venue not in active
        if before is None:
            if cancelled:
                continue
            values = [active[venue] if field == "amount" else row[field]
                      for field in ExpenseFields.model_fields]
            child = db.execute(
                "INSERT INTO chaika.finance_expenses "
                "(department_id,author_id,request_id,allocation_id," + FIELDS + ") "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (venue, row["author_id"], uuid4(), row["id"], *values),
            ).fetchone()
            _record(db, access, "create", child)
        elif not before["cancelled"] or not cancelled:
            # Removed shares retain their own historical value/date, without copying
            # the new group's fields from venues they no longer participate in.
            values = [active[venue] if field == "amount" else row[field]
                      for field in ExpenseFields.model_fields] if not cancelled else [
                          before[field] for field in ExpenseFields.model_fields]
            child = db.execute(
                "UPDATE chaika.finance_expenses SET (" + FIELDS + ")="
                "(%s,%s,%s,%s,%s,%s),cancelled=%s,revision=revision+1 WHERE id=%s RETURNING *",
                (*values, cancelled, before["id"]),
            ).fetchone()
            _record(db, access, "cancel" if cancelled else "update", child, before)


class FinanceAllocations(FinanceLedger):
    def pending(self, scope, start, end, *, limit=100, offset=0):
        with self.repo.connection(repeatable=True) as db:
            access = _access(db, scope, writing=False, all_venues=True)
            _require_accounting(access)
            condition = ("method='pending' AND NOT cancelled AND expense_date BETWEEN %s AND %s "
                         "AND access_department_ids <@ %s::uuid[]")
            params = [start, end, list(access.venues)]
            total = db.execute("SELECT count(*) AS total FROM chaika.finance_allocations WHERE "
                               + condition, params).fetchone()["total"]
            rows = db.execute("SELECT * FROM chaika.finance_allocations WHERE " + condition
                              + " ORDER BY expense_date DESC,created_at DESC,id LIMIT %s OFFSET %s",
                              [*params, limit, offset]).fetchall()
            return {"rows": [_public(row, access) for row in rows], "total": total}

    def get(self, scope, allocation_id):
        with self.repo.connection(repeatable=True) as db:
            access = _access(db, scope, writing=False, all_venues=True)
            return _public(_read(db, access, allocation_id), access)

    def create(self, scope, payload, calculate):
        with self._write(scope, all_venues=True) as (db, access):
            _require_accounting(access)
            # Serialize the request before running time-sensitive revenue calculations.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                       (f"finance-allocation:{access.actor}:{payload.request_id}",))
            original = db.execute(
                "SELECT id,request_payload FROM chaika.finance_allocations "
                "WHERE author_id=%s AND request_id=%s", (access.actor, payload.request_id),
            ).fetchone()
            submitted = _request(payload)
            if original:
                row = _read(db, access, original["id"])
                if original["request_payload"] != submitted:
                    raise HTTPException(409, "Номер запроса уже использован для другого расхода.")
                return _public(row, access)
            _require_parts(access, submitted["parts"])
            parts = _calculated(payload, calculate, db)
            row = db.execute(
                "INSERT INTO chaika.finance_allocations "
                "(author_id,request_id,request_payload,method,parts,access_department_ids,"
                + FIELDS + ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (access.actor, payload.request_id, Jsonb(submitted), payload.method,
                 Jsonb(parts), [UUID(part["department_id"]) for part in parts],
                 *(getattr(payload, field) for field in ExpenseFields.model_fields)),
            ).fetchone()
            _sync_expenses(db, access, row)
            _history(db, access, "create", row)
            return _public(row, access)

    def update(self, scope, allocation_id, payload, calculate):
        return self._change_group(scope, allocation_id, payload, calculate)

    def cancel(self, scope, allocation_id, payload):
        return self._change_group(scope, allocation_id, payload, None)

    def _change_group(self, scope, allocation_id, payload, calculate):
        with self._write(scope, all_venues=True) as (db, access):
            before = _read(db, access, allocation_id, lock=True)
            if before["method"] != "pending":
                _require_parts(access, before["parts"])
            if before["cancelled"] or before["revision"] != payload.revision:
                raise HTTPException(409, "Распределение уже изменено. Обновите журнал.")
            if calculate is None:
                row = db.execute(
                    "UPDATE chaika.finance_allocations SET cancelled=true,revision=revision+1 "
                    "WHERE id=%s RETURNING *", (allocation_id,),
                ).fetchone()
                action = "cancel"
            else:
                _require_parts(access, _request(payload)["parts"])
                parts = _calculated(payload, calculate, db)
                participants = sorted(set(before["access_department_ids"]) |
                                      {UUID(part["department_id"]) for part in parts}, key=str)
                row = db.execute(
                    "UPDATE chaika.finance_allocations SET (" + FIELDS + ")="
                    "(%s,%s,%s,%s,%s,%s),method=%s,parts=%s,access_department_ids=%s,"
                    "revision=revision+1 WHERE id=%s RETURNING *",
                    (*(getattr(payload, field) for field in ExpenseFields.model_fields),
                     payload.method, Jsonb(parts), participants, allocation_id),
                ).fetchone()
                action = "update"
            _sync_expenses(db, access, row)
            _history(db, access, action, row, before)
            return _public(row, access)

    def history(self, scope, allocation_id):
        with self.repo.connection(repeatable=True) as db:
            access = _access(db, scope, writing=False, all_venues=True)
            _read(db, access, allocation_id)
            rows = db.execute(
                "SELECT h.revision,h.action,u.display_name AS actor,h.changed_at,"
                "h.before_state AS before,h.after_state AS after "
                "FROM chaika.finance_allocation_history h "
                "JOIN chaika.web_users u ON u.id=h.actor_id WHERE h.allocation_id=%s "
                "ORDER BY h.revision DESC", (allocation_id,),
            ).fetchall()
            return {"rows": [{**row, "changed_at": row["changed_at"].isoformat()} for row in rows]}
