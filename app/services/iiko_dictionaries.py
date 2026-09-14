"""Strict parsing and local snapshots for the allowlisted reference dictionaries."""

import json
from functools import partial
from pathlib import Path
from uuid import UUID
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.dictionaries import DICTIONARIES, DictionaryKind
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_dictionaries import (
    Account,
    Counteragent,
    DictionaryPage,
    DictionarySnapshot,
    MeasureUnit,
    ProductCategory,
)
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_employees import _value
from app.services.iiko_store_balances import _unique_fields
from app.services.local_catalog import LocalCatalog

MODELS = {
    "accounts": Account,
    "counteragents": Counteragent,
    "measure-units": MeasureUnit,
    "categories": ProductCategory,
}


def read_dictionary(path: Path, max_bytes: int, *, kind: DictionaryKind) -> list:
    if path.stat().st_size > max_bytes:
        raise IikoError("iiko_dictionary_too_large", "Справочник превышает лимит размера.")
    try:
        model = MODELS[kind]
        if kind == "counteragents":
            root = ElementTree.parse(
                path, forbid_dtd=True, forbid_entities=True, forbid_external=True
            ).getroot()
            if root.tag != "employees":
                raise ValueError("Unexpected supplier root")
            names = {field.validation_alias or key for key, field in model.model_fields.items()}
            data = []
            for element in root:
                if element.tag != "employee":
                    raise ValueError("Unexpected supplier element")
                fields = {}
                for field in element:
                    if field.tag not in names:
                        continue  # Private/unknown fields remain in the protected RAW XML only.
                    if field.tag in fields:
                        raise ValueError("Duplicate scalar field")
                    value = _value(field)
                    if field.tag == "representsStore" and value is not None:
                        if value not in {"true", "false", "1", "0"}:
                            raise ValueError("Invalid XML boolean")
                        value = value in {"true", "1"}
                    fields[field.tag] = value
                data.append(fields)
        else:
            data = json.loads(path.read_bytes(), object_pairs_hook=_unique_fields)
            if not isinstance(data, list):
                raise ValueError("Expected array")
        rows = [model.model_validate(item) for item in data]
        if len({r.id for r in rows}) != len(rows):
            raise ValueError("Duplicate identifier")
        return rows
    except (
        ParseError,
        DefusedXmlException,
        ValidationError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        raise IikoError(
            "iiko_dictionary_invalid_response",
            "Ответ справочника повреждён или имеет неверную структуру.",
        ) from None


def summarize_dictionary(rows: list, *, kind: DictionaryKind) -> dict:
    spec = DICTIONARIES[kind]
    return {
        "kind": kind,
        "source_endpoint": spec.endpoint,
        "request": dict(spec.params),
        "deleted_count": sum(row.deleted is True for row in rows),
        "without_code": sum(row.code in {None, ""} for row in rows),
    }


class IikoDictionariesService:
    def __init__(self, settings: Settings, auth: IikoAuthService, directory: Path | None = None):
        base = directory if directory is not None else BACKEND_DIR / ".local/dictionaries"
        self.catalogs = {
            kind: LocalCatalog(
                settings,
                name=f"dictionaries/{kind}",
                directory=base / kind,
                max_bytes=settings.iiko_dictionaries_max_response_bytes,
                download=partial(auth.download_dictionary, kind=kind),
                read=partial(read_dictionary, kind=kind),
                snapshot_model=DictionarySnapshot,
                summarize=partial(summarize_dictionary, kind=kind),
                raw_format=spec.format,
            )
            for kind, spec in DICTIONARIES.items()
        }

    async def load(self, kind: DictionaryKind) -> DictionarySnapshot:
        return await self.catalogs[kind].load()

    async def page(
        self, kind: DictionaryKind, offset: int, limit: int, snapshot_id: UUID | None
    ) -> DictionaryPage:
        snapshot, items = await self.catalogs[kind].page(offset, limit, snapshot_id)
        return DictionaryPage(snapshot=snapshot, offset=offset, limit=limit, items=items)
