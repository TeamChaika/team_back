"""Выгрузка приходных накладных, проверка XML и локальное сохранение ответа."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_invoices import (
    IncomingInvoice,
    IncomingInvoiceItem,
    IncomingInvoicesQuery,
    IncomingInvoicesResponse,
    InvoiceFields,
)
from app.services.iiko_auth import IikoAuthService

logger = logging.getLogger(__name__)


def _fields(element: Element, schema: type[InvoiceFields]) -> dict:
    """Неизвестные поля остаются в RAW; известные поля должны быть однозначными."""
    names = {field.validation_alias or name for name, field in schema.model_fields.items()} - {
        "items"
    }
    result = {}
    for child in element:
        if child.tag not in names:
            continue
        if child.tag in result or len(child):
            raise ValueError("Repeated or nested scalar field")
        result[child.tag] = child.text if child.text is not None else None
    return result


def read_incoming_invoices(path: Path) -> list[IncomingInvoice]:
    try:
        root = ElementTree.parse(path, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        container = root.getroot()
        if container.tag != "incomingInvoiceDtoes":
            raise ValueError("Unexpected XML root")
        documents = []
        seen_ids = set()
        for element in container:
            if element.tag != "document":
                raise ValueError("Unexpected document wrapper")
            values = _fields(element, IncomingInvoice)
            item_containers = element.findall("items")
            if len(item_containers) > 1:
                raise ValueError("Repeated items container")
            items = []
            if item_containers:
                for item in item_containers[0]:
                    if item.tag != "item":
                        raise ValueError("Unexpected item wrapper")
                    items.append(
                        IncomingInvoiceItem.model_validate(_fields(item, IncomingInvoiceItem))
                    )
            values["items"] = items
            document = IncomingInvoice.model_validate(values)
            if document.id in seen_ids:
                raise ValueError("Repeated document UUID")
            seen_ids.add(document.id)
            # Historical iiko exports can repeat num; preserve all lines in XML order.
            documents.append(document)
        return documents
    except (ParseError, DefusedXmlException, ValueError, ValidationError, OverflowError):
        raise IikoError(
            "iiko_invoices_invalid_response",
            "Ответ накладных повреждён или не соответствует ожидаемой структуре XML.",
        ) from None


class IikoInvoicesService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._auth = auth
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._directory = (
            directory if directory is not None else BACKEND_DIR / ".local/incoming-invoices"
        )

    async def get_incoming_invoices(self, query: IncomingInvoicesQuery) -> IncomingInvoicesResponse:
        snapshot_id = uuid4()
        part_file = self._directory / f"{snapshot_id}.xml.part"
        raw_file = self._directory / f"{snapshot_id}.xml"
        meta_file = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            download = await self._auth.download_incoming_invoices(
                part_file,
                date_from=query.date_from,
                date_to=query.date_to,
                supplier_ids=query.supplier_id,
                revision_from=query.revision_from,
            )
            received_at = datetime.now(UTC)
            documents = await asyncio.to_thread(read_incoming_invoices, part_file)
            response = IncomingInvoicesResponse(
                snapshot_id=snapshot_id,
                received_at=received_at,
                request=query,
                total=len(documents),
                items_count=sum(len(document.items) for document in documents),
                documents=documents,
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            metadata = response.model_dump(mode="json", exclude={"documents"})
            metadata["source_fingerprint"] = self._source_fingerprint
            descriptor = os.open(meta_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(metadata, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            part_file.replace(raw_file)
            saved = True
            return response
        except OSError:
            raise IikoError(
                "iiko_invoices_storage_error",
                "Не удалось сохранить ответ накладных локально.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый файл накладных.")
