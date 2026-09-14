"""Полный список складов: проверка XML, локальный снимок и страницы."""

from pathlib import Path
from uuid import UUID
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_stores import IikoStore, IikoStoresPage, IikoStoresSnapshot
from app.services.iiko_auth import IikoAuthService
from app.services.local_catalog import LocalCatalog


def read_stores(path: Path, max_bytes: int) -> list[IikoStore]:
    if path.stat().st_size > max_bytes:
        raise IikoError("iiko_stores_too_large", "Файл складов превышает лимит размера.")
    names = {field.validation_alias or name for name, field in IikoStore.model_fields.items()}
    try:
        root = ElementTree.parse(
            path, forbid_dtd=True, forbid_entities=True, forbid_external=True
        ).getroot()
        if root.tag != "corporateItemDtoes":
            raise ValueError("Unexpected XML root")
        stores: list[IikoStore] = []
        ids: set[UUID] = set()
        for element in root:
            if element.tag != "corporateItemDto":
                raise ValueError("Unexpected store wrapper")
            values = {}
            for field in element:
                if field.tag not in names:
                    continue  # Дополнительные поля остаются в исходном XML.
                if field.tag in values or len(field):
                    raise ValueError("Repeated or nested scalar field")
                value = field.text if field.text is not None else ""
                values[field.tag] = None if field.tag == "parentId" and not value else value
            store = IikoStore.model_validate(values)
            if store.id in ids:
                raise ValueError("Duplicate store UUID")
            ids.add(store.id)
            stores.append(store)
        return stores
    except (ParseError, DefusedXmlException, ValidationError, ValueError, OverflowError):
        raise IikoError(
            "iiko_stores_invalid_response",
            "Ответ складов повреждён или не соответствует ожидаемой структуре XML.",
        ) from None


def summarize_stores(stores: list[IikoStore]) -> dict:
    return {
        "parent_ids": sorted({store.parent_id for store in stores if store.parent_id is not None}),
        "stores_without_code": sum(not store.code for store in stores),
        "stores_without_parent": sum(store.parent_id is None for store in stores),
    }


class IikoStoresService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._catalog = LocalCatalog(
            settings,
            name="stores",
            directory=directory if directory is not None else BACKEND_DIR / ".local/stores",
            max_bytes=settings.iiko_stores_max_response_bytes,
            download=auth.download_stores,
            read=read_stores,
            snapshot_model=IikoStoresSnapshot,
            summarize=summarize_stores,
            raw_format="xml",
        )

    async def load(self) -> IikoStoresSnapshot:
        return await self._catalog.load()

    async def page(
        self, offset: int, limit: int, snapshot_id: UUID | None = None
    ) -> IikoStoresPage:
        snapshot, items = await self._catalog.page(offset, limit, snapshot_id)
        return IikoStoresPage(snapshot=snapshot, offset=offset, limit=limit, items=items)
