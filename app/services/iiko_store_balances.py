"""Остатки товаров на складах: проверка ответа и сохранение исходного JSON."""

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
from app.schemas.iiko_store_balances import (
    StoreBalance,
    StoreBalancesQuery,
    StoreBalancesResponse,
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


def read_store_balances(path: Path, query: StoreBalancesQuery) -> list[StoreBalance]:
    try:
        payload = json.loads(
            path.read_bytes(), parse_float=Decimal, object_pairs_hook=_unique_fields
        )
        if not isinstance(payload, list):
            raise ValueError("Expected JSON array")
        rows = [StoreBalance.model_validate(item) for item in payload]
        for row in rows:
            for field, ids in [
                ("store_id", query.store_id),
                ("product_id", query.product_id),
            ]:
                if ids and getattr(row, field) not in ids:
                    raise ValueError("Source ignored the requested filter")
        return rows
    except (ValueError, ValidationError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_store_balances_invalid_response",
            "Ответ остатков повреждён или не соответствует запрошенным фильтрам.",
        ) from None


class IikoStoreBalancesService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._auth = auth
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._directory = (
            directory if directory is not None else BACKEND_DIR / ".local/store-balances"
        )

    async def get(self, query: StoreBalancesQuery) -> StoreBalancesResponse:
        snapshot_id = uuid4()
        part_file = self._directory / f"{snapshot_id}.json.part"
        raw_file = self._directory / f"{snapshot_id}.json"
        meta_file = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            download = await self._auth.download_store_balances(
                part_file,
                timestamp=query.timestamp,
                store_ids=query.store_id,
                product_ids=query.product_id,
                department_ids=query.department_id,
            )
            received_at = datetime.now(UTC)
            rows = await asyncio.to_thread(read_store_balances, part_file, query)
            response = StoreBalancesResponse(
                snapshot_id=snapshot_id,
                received_at=received_at,
                request=query,
                total=len(rows),
                items=rows,
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            metadata = response.model_dump(mode="json", exclude={"items"})
            metadata["source_fingerprint"] = self._source_fingerprint
            metadata["source_endpoint"] = "v2/reports/balance/stores"
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
                "iiko_store_balances_storage_error",
                "Не удалось сохранить отчёт остатков локально.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый файл отчёта остатков.")
