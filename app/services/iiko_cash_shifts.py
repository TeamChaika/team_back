"""Чтение списка кассовых смен и сохранение неизменённого исходного JSON."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_cash_shifts import CashShift, CashShiftsQuery, CashShiftsResponse
from app.services.iiko_auth import IikoAuthService

logger = logging.getLogger(__name__)
_shifts_adapter = TypeAdapter(list[CashShift])


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def read_cash_shifts(path: Path) -> list[CashShift]:
    try:
        payload = json.loads(
            path.read_bytes(), parse_float=Decimal, object_pairs_hook=_unique_fields
        )
        shifts = _shifts_adapter.validate_python(payload)
        ids = set()
        for shift in shifts:
            if shift.id in ids:
                raise ValueError("Duplicate shift UUID")
            ids.add(shift.id)
        return shifts
    except (ValueError, ValidationError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_cash_shifts_invalid_response",
            "Ответ кассовых смен повреждён или содержит некорректные поля.",
        ) from None


class IikoCashShiftsService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._auth = auth
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._directory = directory if directory is not None else BACKEND_DIR / ".local/cash-shifts"

    async def get(self, query: CashShiftsQuery) -> CashShiftsResponse:
        snapshot_id = uuid4()
        part_file = self._directory / f"{snapshot_id}.json.part"
        raw_file = self._directory / f"{snapshot_id}.json"
        meta_file = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            download = await self._auth.download_cash_shifts(
                part_file,
                open_date_from=query.open_date_from,
                open_date_to=query.open_date_to,
                department_ids=query.department_id,
                group_ids=query.group_id,
                status=query.status,
                revision_from=query.revision_from,
            )
            received_at = datetime.now(UTC)
            shifts = await asyncio.to_thread(read_cash_shifts, part_file)
            response = CashShiftsResponse(
                snapshot_id=snapshot_id,
                received_at=received_at,
                request=query,
                total=len(shifts),
                items=shifts,
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            metadata = response.model_dump(mode="json", exclude={"items"})
            metadata["source_fingerprint"] = self._source_fingerprint
            metadata["source_endpoint"] = "v2/cashshifts/list"
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
                "iiko_cash_shifts_storage_error",
                "Не удалось сохранить кассовые смены локально.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый файл кассовых смен.")
