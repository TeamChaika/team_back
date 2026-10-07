"""Only business fields cross the boundary; legal classification and KPP stay local."""

import hashlib
import json
import re
import unicodedata
from uuid import UUID

from app.documents.policy import identifier, invalid

FIELDS = {"entity_type", "name", "inn", "kpp", "address", "phone", "email"}
LIMITS = {"name": 200, "inn": 12, "kpp": 9, "address": 1000, "phone": 40, "email": 254}


def normalize(body):
    if not isinstance(body, dict) or set(body) - (FIELDS | {"request_id"}):
        invalid("Неизвестные поля контрагента.")
    identifier(body.get("request_id"))
    if not isinstance(body.get("entity_type"), str) or body["entity_type"] not in {
        "organization",
        "ip",
        "person",
    }:
        invalid("Выберите организацию, ИП или физическое лицо.")
    result = {"entity_type": body["entity_type"]}
    for field, limit in LIMITS.items():
        value = body.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            invalid("Проверьте длину полей контрагента.")
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
            invalid("Уберите управляющие символы из реквизитов.")
        result[field] = unicodedata.normalize("NFC", value).strip()
    if not result["name"]:
        invalid("Укажите наименование или ФИО контрагента.")
    inn = result["inn"]
    expected = 10 if result["entity_type"] == "organization" else 12
    if (not inn and result["entity_type"] != "person") or (inn and len(inn) != expected):
        invalid("Укажите ИНН: 10 цифр для организации, 12 для ИП или физлица.")
    if inn and not valid_inn(inn):
        invalid("Проверьте ИНН: контрольные цифры не совпадают.")
    if result["kpp"] and (
        result["entity_type"] != "organization" or not re.fullmatch(r"[0-9]{9}", result["kpp"])
    ):
        invalid("КПП допустим только для организации и должен содержать 9 цифр.")
    if result["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result["email"]):
        invalid("Проверьте адрес электронной почты.")
    if result["phone"] and (
        not re.fullmatch(r"[+0-9() .-]{5,40}", result["phone"])
        or not 5 <= len(phone_key(result["phone"])) <= 15
    ):
        invalid("Проверьте телефон контрагента.")
    return result


def valid_inn(value):
    if not re.fullmatch(r"[0-9]{10}|[0-9]{12}", value) or len(set(value)) == 1:
        return False
    digits = [int(x) for x in value]
    if len(digits) == 10:
        return (
            sum(a * b for a, b in zip(digits, [2, 4, 10, 3, 5, 9, 4, 6, 8], strict=False)) % 11 % 10
            == digits[9]
        )
    first = (
        sum(a * b for a, b in zip(digits, [7, 2, 4, 10, 3, 5, 9, 4, 6, 8], strict=False)) % 11 % 10
    )
    second = (
        sum(a * b for a, b in zip(digits, [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], strict=False))
        % 11
        % 10
    )
    return first == digits[10] and second == digits[11]


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def identity_key(payload):
    if payload["inn"]:
        return "inn:" + payload["inn"]
    contact = (
        phone_key(payload["phone"]) or payload["email"].casefold() or name_key(payload["address"])
    )
    return "name:" + name_key(payload["name"]) + "|contact:" + contact


def name_key(value):
    return " ".join(value.casefold().split())


def phone_key(value):
    return re.sub(r"[^0-9]", "", value)


def code_for(identifier):
    # The public API declares code as string, without a documented length limit.
    # A fixed prefix plus 16 hex digits is compact, stable and checked before POST.
    return "CT" + UUID(str(identifier)).hex[:16]


def wire_fields(payload, code):
    return {
        "code": code,
        "name": payload["name"],
        "taxpayerIdNumber": payload["inn"],
        "address": payload["address"],
        "phone": payload["phone"],
        "email": payload["email"],
        "supplier": "true",
        "employee": "false",
        "client": "false",
        "deleted": "false",
    }


def candidates(payload, registry):
    matches = []
    for row in registry:
        same_inn = bool(payload["inn"] and payload["inn"] == row.get("inn", ""))
        same_name = name_key(payload["name"]) == name_key(row["name"])
        same_contact = any(
            normalize(payload[key]) and normalize(payload[key]) == normalize(row.get(key, ""))
            for key, normalize in (("phone", phone_key), ("email", name_key), ("address", name_key))
        )
        no_contacts = not any(payload[key] or row.get(key) for key in ("phone", "email", "address"))
        if same_inn or (same_name and (same_contact or no_contacts)):
            # Do not expose contact details or any employee DTO fields.
            matches.append({k: row.get(k, "") for k in ("id", "name", "inn", "kpp", "address")})
    return matches[:20]
