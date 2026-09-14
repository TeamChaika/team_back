"""Запрос дневных продаж, проверка периода и сохранение исходного OLAP-ответа."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_olap_sales import DailySalesQuery, DailySalesResponse, DailySalesRow
from app.services.iiko_auth import IikoAuthService

logger = logging.getLogger(__name__)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def read_daily_sales(path: Path, query: DailySalesQuery) -> list[DailySalesRow]:
    try:
        payload = json.loads(
            path.read_bytes(), parse_float=Decimal, object_pairs_hook=_unique_fields
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Expected OLAP data array")
        if payload.get("summary") != []:
            raise ValueError("Unexpected OLAP summary")
        rows = [DailySalesRow.model_validate(item) for item in payload["data"]]
        seen = set()
        for row in rows:
            if row.business_date != query.business_date:
                raise ValueError("Source returned a different accounting day")
            if row.department_id in seen:
                raise ValueError("Repeated department in daily report")
            seen.add(row.department_id)
        return rows
    except (ValueError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_olap_sales_invalid_response",
            "Ответ OLAP повреждён или не соответствует одному дню по подразделениям.",
        ) from None


class IikoDailySalesService:
    def __init__(self, settings: Settings, auth: IikoAuthService, directory: Path | None = None):
        self._auth = auth
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._directory = directory if directory is not None else BACKEND_DIR / ".local/olap-sales"

    async def get(self, query: DailySalesQuery) -> DailySalesResponse:
        snapshot_id = uuid4()
        part_file = self._directory / f"{snapshot_id}.json.part"
        raw_file = self._directory / f"{snapshot_id}.json"
        meta_file = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            download = await self._auth.download_daily_sales(part_file, query=query)
            received_at = datetime.now(UTC)
            rows = await asyncio.to_thread(read_daily_sales, part_file, query)
            response = DailySalesResponse(
                snapshot_id=snapshot_id,
                received_at=received_at,
                request=query,
                total=len(rows),
                items=rows,
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            metadata = response.model_dump(mode="json", exclude={"items"})
            metadata.update(
                {
                    "source_fingerprint": self._source_fingerprint,
                    "source_endpoint": "v2/reports/olap",
                    "source_method": "POST",
                    "source_request": query.iiko_body(),
                }
            )
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
                "iiko_olap_sales_storage_error",
                "Не удалось сохранить дневной отчёт OLAP.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый дневной отчёт OLAP.")
