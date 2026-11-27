"""Owner and accounting maintenance of the expense category and approver lists."""

from contextlib import contextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import Field, model_validator

from app.web.finance_ledger import FinanceLedger, Input, _access


class CategoryCreate(Input):
    name: Annotated[str, Field(min_length=1, max_length=200)]


class CategoryEdit(CategoryCreate):
    revision: int = Field(ge=1)
    archived: bool


class ApproverCreate(CategoryCreate):
    available_to_all: bool
    department_ids: list[UUID] = Field(max_length=100)

    @model_validator(mode="after")
    def valid_assignment(self):
        if self.available_to_all == bool(self.department_ids):
            raise ValueError("Выберите все заведения или перечислите отдельные заведения.")
        if len(set(self.department_ids)) != len(self.department_ids):
            raise ValueError("Заведение указано дважды.")
        return self


class ApproverEdit(ApproverCreate):
    revision: int = Field(ge=1)
    archived: bool


CATEGORY_FIELDS = "code,name,archived,revision"
APPROVERS = (
    "SELECT p.id,p.name,p.available_to_all,p.archived,p.revision,"
    "ARRAY(SELECT d.department_id FROM chaika.finance_department_approvers d "
    "WHERE d.approver_id=p.id AND d.source_id='primary' ORDER BY d.department_id) "
    "AS department_ids FROM chaika.finance_approvers p"
)


def _admin(access):
    if access.level not in {"accountant", "owner"}:
        raise HTTPException(403, "Справочники доступны бухгалтерии и владельцу.")


def _global_access(db, access):
    return db.execute(
        "SELECT all_departments FROM chaika.web_users WHERE id=%s", (access.actor,)
    ).fetchone()["all_departments"]


def _visible(row, venues, global_access):
    assigned = set(row["department_ids"])
    visible = assigned.intersection(venues)
    if not (global_access or row["available_to_all"] or visible):
        return None
    return {
        **row,
        # Old imports can include redundant links for a network-wide approver.
        "department_ids": [] if row["available_to_all"] else sorted(visible, key=str),
        "can_edit": global_access or (not row["available_to_all"] and assigned == visible),
    }


def _revision(row, revision):
    if row["revision"] != revision:
        raise HTTPException(409, "Запись уже изменена. Обновите справочник.")


def _category_name_available(db, name, code=None):
    if db.execute(
        "SELECT 1 FROM chaika.finance_categories WHERE NOT archived "
        "AND lower(btrim(name))=lower(%s) AND code IS DISTINCT FROM %s",
        (name, code),
    ).fetchone():
        raise HTTPException(409, "Активная статья с таким названием уже есть.")


class FinanceCatalog(FinanceLedger):
    @contextmanager
    def _edit(self, scope):
        with self._write(scope, all_venues=True) as (db, access):
            _admin(access)
            # Expense writers share this lock while checking names and assignments.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended('chaika:finance-catalog',0))")
            yield db, access

    def categories(self, scope):
        with self.repo.connection(repeatable=True) as db:
            _access(db, scope, writing=False)
            return {
                "rows": db.execute(
                    "SELECT code,name FROM chaika.finance_categories WHERE NOT archived "
                    "ORDER BY sort_order,name,code"
                ).fetchall()
            }

    def catalog(self, scope):
        with self.repo.connection(repeatable=True) as db:
            access = _access(db, scope, writing=False, all_venues=True)
            _admin(access)
            global_access = _global_access(db, access)
            people = db.execute(APPROVERS + " ORDER BY p.archived,p.name,p.id").fetchall()
            return {
                "categories": db.execute(
                    "SELECT " + CATEGORY_FIELDS + " FROM chaika.finance_categories "
                    "ORDER BY archived,sort_order,name,code"
                ).fetchall(),
                "approvers": [
                    person
                    for row in people
                    if (person := _visible(row, access.venues, global_access)) is not None
                ],
                "venues": sorted(
                    access.venues.values(), key=lambda venue: (venue["name"], venue["id"])
                ),
                "can_manage_global_approvers": global_access,
            }

    def create_category(self, scope, payload):
        with self._edit(scope) as (db, _):
            _category_name_available(db, payload.name)
            return db.execute(
                "INSERT INTO chaika.finance_categories(code,name) VALUES(%s,%s) RETURNING "
                + CATEGORY_FIELDS,
                ("cat_" + uuid4().hex, payload.name),
            ).fetchone()

    def edit_category(self, scope, code, payload):
        with self._edit(scope) as (db, _):
            row = db.execute(
                "SELECT revision FROM chaika.finance_categories WHERE code=%s FOR UPDATE", (code,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Статья расходов не найдена.")
            _revision(row, payload.revision)
            if not payload.archived:
                _category_name_available(db, payload.name, code)
            return db.execute(
                "UPDATE chaika.finance_categories SET name=%s,archived=%s,revision=revision+1 "
                "WHERE code=%s RETURNING " + CATEGORY_FIELDS,
                (payload.name, payload.archived, code),
            ).fetchone()

    def save_approver(self, scope, payload, person_id=None):
        with self._edit(scope) as (db, access):
            global_access = _global_access(db, access)
            if person_id is not None:
                old = db.execute(
                    APPROVERS + " WHERE p.id=%s FOR UPDATE OF p", (person_id,)
                ).fetchone()
                visible = _visible(old, access.venues, global_access) if old else None
                if visible is None:
                    raise HTTPException(404, "Согласующий не найден.")
                if not visible["can_edit"]:
                    raise HTTPException(403, "Изменение затрагивает заведения вне вашего доступа.")
                _revision(old, payload.revision)

            if payload.available_to_all and not global_access:
                raise HTTPException(403, "Для общего согласующего нужен доступ ко всем заведениям.")
            if not set(payload.department_ids).issubset(access.venues):
                raise HTTPException(403, "Нет доступа к выбранным заведениям.")
            archived = isinstance(payload, ApproverEdit) and payload.archived
            if (
                not archived
                and db.execute(
                    "SELECT 1 FROM chaika.finance_approvers p WHERE NOT p.archived "
                    "AND p.id IS DISTINCT FROM %s AND lower(btrim(p.name))=lower(%s) "
                    "AND (%s OR p.available_to_all OR EXISTS (SELECT FROM "
                    "chaika.finance_department_approvers d WHERE d.approver_id=p.id "
                    "AND d.source_id='primary' AND d.department_id=ANY(%s::uuid[])))",
                    (person_id, payload.name, payload.available_to_all, payload.department_ids),
                ).fetchone()
            ):
                raise HTTPException(
                    409, "В выбранных заведениях уже есть согласующий с таким именем."
                )
            if person_id is None:
                person_id = db.execute(
                    "INSERT INTO chaika.finance_approvers(name,available_to_all) "
                    "VALUES(%s,%s) RETURNING id",
                    (payload.name, payload.available_to_all),
                ).fetchone()["id"]
            else:
                db.execute(
                    "UPDATE chaika.finance_approvers SET name=%s,available_to_all=%s,archived=%s,"
                    "revision=revision+1 WHERE id=%s",
                    (payload.name, payload.available_to_all, archived, person_id),
                )
                db.execute(
                    "DELETE FROM chaika.finance_department_approvers "
                    "WHERE source_id='primary' AND approver_id=%s",
                    (person_id,),
                )
            if payload.department_ids:
                db.execute(
                    "INSERT INTO chaika.finance_department_approvers(approver_id,department_id) "
                    "SELECT %s,department_id FROM unnest(%s::uuid[]) AS d(department_id)",
                    (person_id, payload.department_ids),
                )
            row = db.execute(APPROVERS + " WHERE p.id=%s", (person_id,)).fetchone()
            return _visible(row, access.venues, global_access)
