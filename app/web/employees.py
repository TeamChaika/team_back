"""Owner-only employee commands, durable deduplication and iiko read-back."""

import asyncio
import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID, uuid4
from xml.etree.ElementTree import Element, ParseError, tostring

import psycopg
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from fastapi import HTTPException
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.integrations.iiko.employee_write import EmployeeGateway
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_employees import IikoEmployee
from app.services.iiko_employees import read_employees
from app.sync_references import SyncError, append_snapshot, reference_lock
from app.web.repository import serial

Text = Annotated[str, Field(max_length=200)]
Code = Annotated[str, Field(min_length=1, max_length=100)]
ALIASES = {
    "name": "name",
    "code": "code",
    "first_name": "firstName",
    "last_name": "lastName",
    "middle_name": "middleName",
    "phone": "phone",
    "cell_phone": "cellPhone",
    "email": "email",
    "main_role_code": "mainRoleCode",
    "role_codes": "roleCodes",
    "preferred_department_code": "preferredDepartmentCode",
    "department_codes": "departmentCodes",
    "card_number": "cardNumber",
}


class EmployeeFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    code: Code | None = None
    first_name: Text | None = None
    middle_name: Text | None = None
    last_name: Text | None = None
    phone: Text | None = None
    cell_phone: Text | None = None
    email: Text | None = None
    main_role_code: Code | None = None
    role_codes: Annotated[list[Code], Field(min_length=1, max_length=100)] | None = None
    preferred_department_code: Text | None = None
    department_codes: Annotated[list[Code], Field(min_length=1, max_length=100)] | None = None
    card_number: Annotated[str, Field(max_length=200)] | None = None
    pin_code: SecretStr | None = None

    @field_validator("pin_code", mode="before")
    @classmethod
    def validate_pin(cls, value):
        pin = value.get_secret_value() if isinstance(value, SecretStr) else value
        if (
            not isinstance(pin, str)
            or not pin
            or len(pin) > 32
            or not pin.isascii()
            or not pin.isdigit()
        ):
            raise ValueError("PIN must contain 1 to 32 digits")
        return value

    @model_validator(mode="after")
    def validate_values(self):
        values = self.model_dump(exclude_unset=True)
        if not values or any(v is None for v in values.values()):
            raise ValueError("Provide changed values, not nulls")
        for value in values.values():
            if isinstance(value, SecretStr):
                continue
            for text in value if isinstance(value, list) else [value]:
                if any(ord(c) < 32 for c in text):
                    raise ValueError("Control characters are not allowed")
        if self.email and ("@" not in self.email or any(c.isspace() for c in self.email)):
            raise ValueError("Invalid email")
        return self


class EmployeeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    request_id: UUID
    version: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    fields: EmployeeFields


class EmployeeCard(IikoEmployee):
    """Card number is available only in the owner editor, not the public catalog."""

    card_number: str = ""


def owner(scope):
    if scope.user["role"] != "owner":
        raise HTTPException(403, "Добавление и редактирование сотрудников доступно владельцу.")


def pending_error(message):
    return HTTPException(409, {"message": message, "employee_pending": True})


def parse_card(raw, employee_id):
    if raw is None:
        raise HTTPException(404, "Сотрудник не найден в iiko.")
    try:
        root = fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if root.tag != "employee" or root.findtext("id") != str(employee_id):
            raise ValueError("Unexpected employee")
        # System accounts, suppliers and deleted cards are not editable through this form.
        if root.findtext("employee") not in {"true", "1"} or root.findtext("deleted") in {
            "true",
            "1",
        }:
            raise HTTPException(409, "Эта карточка недоступна для редактирования сотрудника.")
        wrapper = Element("employees")
        wrapper.append(root)
        with TemporaryDirectory(prefix="chaika-employee-") as directory:
            path = Path(directory) / "employee.xml"
            path.write_bytes(tostring(wrapper))
            item = read_employees(path, 1024 * 1024)[0]
        return EmployeeCard.model_construct(
            **item.model_dump(), card_number=root.findtext("cardNumber") or ""
        )
    except (ParseError, DefusedXmlException, ValueError):
        raise IikoError(
            "employee_invalid_response", "iiko вернул некорректную карточку сотрудника."
        ) from None


def version(item):
    return hashlib.sha256(item.model_dump_json().encode()).hexdigest()


def form_card(item):
    data = item.model_dump(mode="json")
    return dict(id=str(item.id), version=version(item), fields={k: data[k] for k in ALIASES})


def matches(item, fields):
    data = item.model_dump(mode="json")
    return all(
        sorted(data.get(k) or []) == sorted(v) if isinstance(v, list) else (data.get(k) or "") == v
        for k, v in fields.items()
        if k != "pin_code"
    )


def publish_one(db, raw, item):
    """Single-card observation never marks other employees absent."""
    run_id, snapshot_id, observed = uuid4(), uuid4(), datetime.now(UTC)
    # Credentials must not appear in catalog details or saved RAW responses.
    root = fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    for tag in ("password", "pinCode", "cardNumber"):
        for node in root.findall(tag):
            root.remove(node)
    raw = tostring(root)
    item = IikoEmployee.model_validate(item.model_dump(), by_name=True)
    data = item.model_dump(mode="json")
    with db.transaction():
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status,finished_at,counts) "
            "VALUES(%s,'employees','succeeded',%s,%s)",
            (run_id, observed, Jsonb({"scope": "single_employee", "employees_updated": 1})),
        )
        append_snapshot(
            db,
            run_id,
            dict(
                id=snapshot_id,
                source_id="primary",
                resource="employees",
                observed_at=observed,
                sha256=hashlib.sha256(raw).hexdigest(),
                raw=raw,
                payload={
                    "scope": "single_employee",
                    "items": [data],
                    "redacted_fields": ["password", "pinCode", "cardNumber"],
                },
            ),
        )
        row = dict(
            source_id="primary",
            **item.model_dump(),
            present_in_latest=True,
            first_seen_at=observed,
            last_seen_at=observed,
            last_snapshot_id=snapshot_id,
            details=Jsonb(data),
        )
        columns = list(row)
        updates = [c for c in columns if c not in {"id", "source_id", "first_seen_at"}]
        db.execute(
            sql.SQL(
                "INSERT INTO chaika.employees ({}) VALUES ({}) ON CONFLICT "
                "(source_id,id) DO UPDATE SET {}"
            ).format(
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns),
                sql.SQL(",").join(
                    sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                    for c in updates
                ),
            ),
            list(row.values()),
        )
    return snapshot_id


class EmployeeEditor:
    def __init__(self, settings, repo, *, gateway=EmployeeGateway):
        self.settings, self.repo, self.gateway = settings, repo, gateway

    def options(self, scope):
        owner(scope)
        with self.repo.connection() as db:
            roles = db.execute(
                "SELECT code,name FROM chaika.employee_roles WHERE source_id='primary' "
                "AND present_in_latest AND NOT coalesce(deleted,false) AND code<>'' "
                "AND code IN (SELECT code FROM chaika.employee_roles WHERE source_id='primary' "
                "GROUP BY code HAVING count(*)=1) ORDER BY name"
            ).fetchall()
        return serial({"roles": roles, "departments": list(scope.departments)})

    def pending(self, scope):
        owner(scope)
        with self.repo.connection() as db:
            return serial(
                db.execute(
                    "SELECT id,employee_id,fields->>'name' AS name,created_at "
                    "FROM chaika.employee_changes WHERE status='pending' AND user_id=%s "
                    "ORDER BY created_at DESC LIMIT 20",
                    (scope.user["id"],),
                ).fetchall()
            )

    def reconcile(self, scope, request_id):
        """User-requested import of actual state, including partly accepted changes."""
        with self.connection(scope) as db:
            with db.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute(
                    "SELECT * FROM chaika.employee_changes WHERE id=%s AND user_id=%s",
                    (request_id, scope.user["id"]),
                ).fetchone()
            if not row:
                raise HTTPException(404, "Сохранение не найдено.")
            if row["status"] != "pending":
                return {"id": str(row["employee_id"]), "status": row["status"]}

            async def collect():
                async with self.gateway(self.settings) as gateway:
                    raw = await gateway.get(row["employee_id"])
                    item = parse_card(raw, row["employee_id"])
                    pin_unknown = "pin_code" in row["fields"] and not row["pin_accepted"]
                    status = (
                        "confirmed"
                        if matches(item, row["fields"]) and not pin_unknown
                        else "reconciled"
                    )
                    with db.transaction():
                        snapshot_id = publish_one(db, raw, item)
                        db.execute(
                            "UPDATE chaika.employee_changes SET status=%s,snapshot_id=%s,"
                            "finished_at=now() WHERE id=%s",
                            (status, snapshot_id, request_id),
                        )
                    return {"id": str(item.id), "status": status, "pin_unknown": pin_unknown}

            return asyncio.run(collect())

    @contextmanager
    def connection(self, scope):
        owner(scope)
        if not self.settings.iiko_configured:
            raise HTTPException(503, "Подключение к iiko не настроено.")
        try:
            with psycopg.connect(
                self.settings.database_url.get_secret_value(),
                autocommit=True,
                connect_timeout=10,
                application_name="chaika-employee-edit",
            ) as db:
                with reference_lock(db):
                    # Recheck privileges after acquiring the shared collector lock.
                    row = db.execute(
                        "SELECT role FROM chaika.web_users WHERE id=%s AND active",
                        (scope.user["id"],),
                    ).fetchone()
                    if not row or row[0] != "owner":
                        raise HTTPException(403, "Нет права редактирования сотрудников.")
                    expected = hashlib.sha256(
                        f"{str(self.settings.iiko_base_url).rstrip('/')}\n{self.settings.iiko_login}".encode()
                    ).hexdigest()
                    source = db.execute(
                        "SELECT fingerprint FROM chaika.sources WHERE id='primary'"
                    ).fetchone()
                    if not source or source[0] != expected:
                        raise HTTPException(
                            503, "Источник сотрудников не совпадает с настройками iiko."
                        )
                    yield db
        except SyncError:
            raise HTTPException(
                409, "Сейчас выполняется синхронизация iiko. Повторите через минуту."
            ) from None
        except IikoError as exc:
            raise HTTPException(exc.status_code, exc.message) from None

    def read(self, scope, employee_id):
        owner(scope)
        self.repo.detail(scope, "employees", employee_id)
        with self.connection(scope):

            async def collect():
                async with self.gateway(self.settings) as gateway:
                    return form_card(parse_card(await gateway.get(employee_id), employee_id))

            return asyncio.run(collect())

    def save(self, scope, payload, employee_id=None):
        owner(scope)
        if employee_id:
            self.repo.detail(scope, "employees", employee_id)
        fields = payload.fields.model_dump(exclude_unset=True)
        pin = fields.pop("pin_code", None)
        if pin is not None:
            fields["pin_code"] = True  # Audit marker only; never persist the PIN.
        signature_data = json.dumps(
            {"id": str(employee_id), "fields": fields, "version": payload.version},
            sort_keys=True,
        ).encode()
        if pin is not None:
            signature = hmac.new(
                self.settings.iiko_password.get_secret_value().encode(),
                signature_data + b"\x00" + pin.get_secret_value().encode(),
                hashlib.sha256,
            ).hexdigest()
        else:
            signature = hashlib.sha256(signature_data).hexdigest()
        with self.connection(scope) as db:
            with db.cursor(row_factory=dict_row) as cursor:
                old = cursor.execute(
                    "SELECT * FROM chaika.employee_changes WHERE id=%s", (payload.request_id,)
                ).fetchone()
            if old and (old["user_id"] != scope.user["id"] or old["request_hash"] != signature):
                raise HTTPException(409, "Идентификатор сохранения уже использован.")
            if old and old["status"] == "confirmed":
                return {"id": str(old["employee_id"]), "status": "confirmed"}
            if old and old["status"] == "reconciled":
                raise HTTPException(
                    422, "Данные повторно загружены из iiko. Откройте карточку заново."
                )
            if old and old["status"] == "rejected":
                raise HTTPException(422, "iiko отклонил это сохранение. Откройте карточку заново.")
            target = old["employee_id"] if old else employee_id or uuid4()

            async def execute():
                async with self.gateway(self.settings) as gateway:
                    raw = await gateway.get(target)
                    current = parse_card(raw, target) if raw else None
                    pin_accepted = bool(old and old["pin_accepted"])
                    if old:
                        if (
                            not current
                            or not matches(current, fields)
                            or (pin and not pin_accepted)
                        ):
                            raise pending_error(
                                "Результат сохранения ещё не подтверждён в iiko. "
                                "Повторная запись не отправлена. "
                                "Если меняли PIN, проверьте вход в iikoFront.",
                            )
                    else:
                        if employee_id and (not current or version(current) != payload.version):
                            raise HTTPException(
                                409, "Карточка изменилась в iiko. Откройте её заново."
                            )
                        if not employee_id and current:
                            raise HTTPException(409, "Идентификатор сотрудника уже занят.")
                        self.validate(db, fields, current)
                        pending = db.execute(
                            "SELECT id FROM chaika.employee_changes WHERE employee_id=%s "
                            "AND status='pending'",
                            (target,),
                        ).fetchone()
                        if pending:
                            raise HTTPException(
                                409,
                                "У сотрудника есть неподтверждённое сохранение. "
                                "Проверьте результат предыдущего запроса.",
                            )
                        # The autocommit reservation survives timeouts, disconnects and restarts.
                        db.execute(
                            "INSERT INTO chaika.employee_changes "
                            "(id,user_id,employee_id,is_create,request_hash,"
                            "fields,before_fields,status) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,'pending')",
                            (
                                payload.request_id,
                                scope.user["id"],
                                target,
                                employee_id is None,
                                signature,
                                Jsonb(fields),
                                Jsonb(form_card(current)["fields"]) if current else None,
                            ),
                        )
                        wire = {ALIASES[k]: v for k, v in fields.items() if k != "pin_code"}
                        if pin is not None:
                            wire["pinCode"] = pin.get_secret_value()
                        if not employee_id:
                            wire.update(
                                employee="true", deleted="false", supplier="false", client="false"
                            )
                        try:
                            await gateway.save(target, wire)
                            if pin is not None:
                                db.execute(
                                    "UPDATE chaika.employee_changes SET pin_accepted=true "
                                    "WHERE id=%s",
                                    (payload.request_id,),
                                )
                                pin_accepted = True
                        except IikoError as exc:
                            if not exc.outcome_unknown and exc.upstream_status_code in {
                                400,
                                401,
                                403,
                                404,
                                409,
                                422,
                            }:
                                db.execute(
                                    "UPDATE chaika.employee_changes SET status='rejected', "
                                    "finished_at=now(),error_code=%s WHERE id=%s",
                                    (exc.code, payload.request_id),
                                )
                                raise
                            # A write may already have succeeded. Only GET is allowed below.
                        raw = await gateway.get(target)
                        current = parse_card(raw, target) if raw else None
                        if (
                            not current
                            or not matches(current, fields)
                            or (pin and not pin_accepted)
                        ):
                            raise pending_error(
                                "iiko пока не подтвердил все изменения. "
                                "Нажмите «Проверить сохранение»; повторной записи не будет. "
                                + ("PIN проверьте при входе в iikoFront." if pin else ""),
                            )
                    with db.transaction():
                        snapshot_id = publish_one(db, raw, current)
                        db.execute(
                            "UPDATE chaika.employee_changes SET status='confirmed',"
                            "finished_at=now(), "
                            "snapshot_id=%s WHERE id=%s",
                            (snapshot_id, payload.request_id),
                        )
                    return {"id": str(target), "status": "confirmed"}

            return asyncio.run(execute())

    def validate(self, db, fields, current):
        if current is None and not all(
            fields.get(k)
            for k in ("name", "code", "main_role_code", "role_codes", "department_codes")
        ):
            raise HTTPException(422, "Укажите имя, табельный номер, должность и заведения.")
        if "code" in fields and (current is None or fields["code"] != current.code):
            if db.execute(
                "SELECT 1 FROM chaika.employee_changes WHERE status='pending' "
                "AND fields->>'code'=%s LIMIT 1",
                (fields["code"],),
            ).fetchone():
                raise HTTPException(
                    409,
                    "Для этого табельного номера уже отправлено сохранение. "
                    "Сначала загрузите его текущие данные из iiko.",
                )
            if db.execute(
                "SELECT 1 FROM chaika.employees WHERE source_id='primary' AND code=%s "
                "AND id<>%s LIMIT 1",
                (fields["code"], current.id if current else UUID(int=0)),
            ).fetchone():
                raise HTTPException(409, "Такой табельный номер уже есть. Используйте другой.")
        data = current.model_dump() if current else {}
        data.update(fields)
        for column, table, codes in [
            (
                "code",
                "employee_roles",
                set(fields.get("role_codes", []))
                | ({fields["main_role_code"]} if fields.get("main_role_code") else set()),
            ),
            (
                "code",
                "corporate_nodes",
                set(fields.get("department_codes", []))
                | (
                    {fields["preferred_department_code"]}
                    if fields.get("preferred_department_code")
                    else set()
                ),
            ),
        ]:
            for code in codes:
                count = db.execute(
                    sql.SQL(
                        "SELECT count(*) FROM chaika.{} WHERE source_id='primary' AND {}=%s"
                    ).format(sql.Identifier(table), sql.Identifier(column)),
                    (code,),
                ).fetchone()[0]
                if count != 1:
                    raise HTTPException(
                        422, "Должность или заведение не найдено либо код неоднозначен."
                    )
        if any(k in fields for k in ["main_role_code", "role_codes"]) and data.get(
            "main_role_code"
        ) not in (data.get("role_codes") or []):
            raise HTTPException(422, "Основная должность должна входить в список должностей.")
        if any(k in fields for k in ["preferred_department_code", "department_codes"]) and data.get(
            "preferred_department_code"
        ):
            departments = data.get("department_codes")
            if departments and data["preferred_department_code"] not in departments:
                raise HTTPException(422, "Основное заведение должно входить в выбранные заведения.")
