"""Внутренние перемещения: проверка JSON-обёртки, документов и сохранение исходного ответа."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_transfers import (
    TransferExport,
    TransfersQuery,
    TransfersResponse,
)
from app.services.iiko_auth import IikoAuthService

logger = logging.getLogger(__name__)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def read_transfers(path: Path, query: TransfersQuery) -> TransferExport:
    try:
        payload = json.loads(
            path.read_bytes(), parse_float=Decimal, object_pairs_hook=_unique_fields
        )
        if not isinstance(payload, dict):
            raise ValueError("Expected export envelope")
        if not isinstance(payload.get("result"), str) or not isinstance(
            payload.get("errors"), list
        ):
            raise ValueError("Missing or malformed export status")
        if payload["result"] != "SUCCESS" or payload["errors"]:
            raise IikoError(
                "iiko_transfers_export_failed",
                "iiko сообщил об ошибке выгрузки внутренних перемещений.",
            )
        export = TransferExport.model_validate(payload)
        ids = set()
        for document in export.response:
            if document.id in ids:
                raise ValueError("Duplicate document UUID")
            ids.add(document.id)
            if not query.date_from <= document.date_incoming.date() <= query.date_to:
                raise ValueError("Document outside requested accounting period")
            if query.status is not None and document.status != query.status:
                raise ValueError("Source ignored status filter")
            if len({item.num for item in document.items}) != len(document.items):
                raise ValueError("Duplicate line numbers")
        return export
    except (ValueError, ValidationError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_transfers_invalid_response",
            "Ответ внутренних перемещений повреждён или не соответствует запросу.",
        ) from None


class IikoTransfersService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._auth = auth
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._directory = directory if directory is not None else BACKEND_DIR / ".local/transfers"

    async def get(self, query: TransfersQuery) -> TransfersResponse:
        snapshot_id = uuid4()
        part_file = self._directory / f"{snapshot_id}.json.part"
        raw_file = self._directory / f"{snapshot_id}.json"
        meta_file = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            download = await self._auth.download_transfers(
                part_file,
                date_from=query.date_from,
                date_to=query.date_to,
                status=query.status,
                revision_from=query.revision_from,
            )
            received_at = datetime.now(UTC)
            export = await asyncio.to_thread(read_transfers, part_file, query)
            response = TransfersResponse(
                snapshot_id=snapshot_id,
                received_at=received_at,
                request=query,
                revision=export.revision,
                total=len(export.response),
                items_count=sum(len(document.items) for document in export.response),
                documents=export.response,
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            metadata = response.model_dump(mode="json", exclude={"documents"})
            metadata["source_fingerprint"] = self._source_fingerprint
            metadata["source_endpoint"] = "v2/documents/internalTransfer"
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
                "iiko_transfers_storage_error",
                "Не удалось сохранить внутренние перемещения локально.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый файл внутренних перемещений.")
