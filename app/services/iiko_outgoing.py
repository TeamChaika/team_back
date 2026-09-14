"""Выгрузка расходных накладных, проверка XML и локальное сохранение ответа."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_outgoing import (
    OutgoingInvoice,
    OutgoingInvoiceItem,
    OutgoingInvoicesQuery,
    OutgoingInvoicesResponse,
)
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_invoices import _fields

logger = logging.getLogger(__name__)


def read_outgoing_invoices(
    path: Path, query: OutgoingInvoicesQuery | None = None
) -> list[OutgoingInvoice]:
    try:
        root = ElementTree.parse(path, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        container = root.getroot()
        if container.tag != "outgoingInvoiceDtoes":
            raise ValueError("Unexpected XML root")
        documents = []
        seen_ids = set()
        for element in container:
            if element.tag != "document":
                raise ValueError("Unexpected document wrapper")
            values = _fields(element, OutgoingInvoice)
            item_containers = element.findall("items")
            if len(item_containers) > 1:
                raise ValueError("Repeated items container")
            items = []
            if item_containers:
                for item in item_containers[0]:
                    if item.tag != "item":
                        raise ValueError("Unexpected item wrapper")
                    items.append(
                        OutgoingInvoiceItem.model_validate(_fields(item, OutgoingInvoiceItem))
                    )
            values["items"] = items
            document = OutgoingInvoice.model_validate(values)
            if document.id in seen_ids:
                raise ValueError("Repeated document UUID")
            seen_ids.add(document.id)
            if query is not None:
                value = document.date_incoming
                day = value.date() if isinstance(value, datetime) else value
                if not query.date_from <= day <= query.date_to:
                    raise ValueError("Source returned an out-of-range document")
            # No source line number: preserve repeated items in their original array order.
            documents.append(document)
        return documents
    except (ParseError, DefusedXmlException, ValueError, ValidationError, OverflowError):
        raise IikoError(
            "iiko_outgoing_invalid_response",
            "Ответ накладных повреждён или не соответствует ожидаемой структуре XML.",
        ) from None


class IikoOutgoingInvoicesService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._auth = auth
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._directory = (
            directory if directory is not None else BACKEND_DIR / ".local/outgoing-invoices"
        )

    async def get_outgoing_invoices(self, query: OutgoingInvoicesQuery) -> OutgoingInvoicesResponse:
        snapshot_id = uuid4()
        part_file = self._directory / f"{snapshot_id}.xml.part"
        raw_file = self._directory / f"{snapshot_id}.xml"
        meta_file = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            download = await self._auth.download_outgoing_invoices(
                part_file,
                date_from=query.date_from,
                date_to=query.date_to,
            )
            received_at = datetime.now(UTC)
            documents = await asyncio.to_thread(read_outgoing_invoices, part_file, query)
            response = OutgoingInvoicesResponse(
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
            metadata["source_endpoint"] = "documents/export/outgoingInvoice"
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
                "iiko_outgoing_storage_error",
                "Не удалось сохранить ответ накладных локально.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый файл накладных.")
