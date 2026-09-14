"""Один запрос getAssembled и локальное сохранение его исходного ответа."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_assembly import (
    AllAssemblyResponse,
    AllAssemblySourceResponse,
    AssembledChartResponse,
    AssembledSourceResponse,
    AssemblyChart,
)
from app.services.iiko_auth import IikoAuthService

logger = logging.getLogger(__name__)


def read_all_assembly(path: Path, business_date: date, max_bytes: int) -> AllAssemblySourceResponse:
    try:
        if path.stat().st_size > max_bytes:
            raise ValueError("Response too large")

        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result

        source = json.loads(path.read_bytes(), parse_float=Decimal, object_pairs_hook=unique_fields)
        result = AllAssemblySourceResponse.model_validate(source)
        ids = set()
        for chart in result.assembly_charts:
            if chart.id in ids or chart.date_from > business_date:
                raise ValueError("Duplicate or future chart")
            ids.add(chart.id)
            if chart.date_to is not None and (
                chart.date_to <= business_date or chart.date_to <= chart.date_from
            ):
                raise ValueError("Chart outside requested day")
            if len({item.id for item in chart.items}) != len(chart.items):
                raise ValueError("Duplicate ingredient ID")
        return result
    except (ValueError, ValidationError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_all_assembly_invalid_response", "Некорректная массовая выгрузка техкарт."
        ) from None


def read_assembled(path: Path, product_id: UUID, business_date: date) -> AssemblyChart | None:
    try:
        source = json.loads(path.read_bytes(), parse_float=Decimal)
        result = AssembledSourceResponse.model_validate(source)
        chart = result.assembly_charts[0] if result.assembly_charts else None
        if chart is not None:
            if chart.assembled_product_id != product_id:
                raise ValueError("Wrong product")
            if chart.date_from > business_date or (
                chart.date_to is not None and business_date >= chart.date_to
            ):
                raise ValueError("Chart is not effective on the requested date")
            if len({item.id for item in chart.items}) != len(chart.items):
                raise ValueError("Duplicate chart item IDs")
        return chart
    except (ValueError, ValidationError, OverflowError):
        raise IikoError(
            "iiko_assembly_invalid_response",
            "Ответ техкарты повреждён или не соответствует запрошенному блюду и дате.",
        ) from None


class IikoAssemblyService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._auth = auth
        self._all_max_bytes = settings.iiko_all_assembly_max_response_bytes
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._directory = (
            directory if directory is not None else BACKEND_DIR / ".local/assembly-charts"
        )

    async def get_all(self, business_date: date) -> AllAssemblyResponse:
        snapshot_id = uuid4()
        part = self._directory / f"{snapshot_id}.json.part"
        raw = self._directory / f"{snapshot_id}.json"
        meta = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            download = await self._auth.download_all_assembly(part, business_date=business_date)
            export = await asyncio.to_thread(
                read_all_assembly, part, business_date, self._all_max_bytes
            )
            response = AllAssemblyResponse(
                snapshot_id=snapshot_id,
                received_at=datetime.now(UTC),
                business_date=business_date,
                date_to_exclusive=business_date + timedelta(days=1),
                known_revision=export.known_revision,
                total=len(export.assembly_charts),
                items_count=sum(len(c.items) for c in export.assembly_charts),
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            metadata = response.model_dump(mode="json")
            metadata.update(
                source_fingerprint=self._source_fingerprint,
                source_endpoint="v2/assemblyCharts/getAll",
            )
            fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(metadata, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            part.replace(raw)
            saved = True
            return response
        except OSError:
            raise IikoError(
                "iiko_all_assembly_storage_error", "Не удалось сохранить техкарты.", status_code=500
            ) from None
        finally:
            for path in [part] if saved else [part, meta]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось убрать незавершённый файл массовой выгрузки.")

    async def get_assembled(
        self, product_id: UUID, business_date: date, department_id: UUID | None = None
    ) -> AssembledChartResponse:
        snapshot_id = uuid4()
        part_file = self._directory / f"{snapshot_id}.json.part"
        raw_file = self._directory / f"{snapshot_id}.json"
        meta_file = self._directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            download = await self._auth.download_assembled(
                part_file,
                product_id=product_id,
                business_date=business_date,
                department_id=department_id,
            )
            received_at = datetime.now(UTC)
            chart = await asyncio.to_thread(read_assembled, part_file, product_id, business_date)
            response = AssembledChartResponse(
                snapshot_id=snapshot_id,
                received_at=received_at,
                product_id=product_id,
                business_date=business_date,
                department_id=department_id,
                chart=chart,
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            # Сохраняем параметры рядом с исходным JSON: дата запроса не равна дате начала карты.
            metadata = response.model_dump(mode="json", exclude={"chart"})
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
                "iiko_assembly_storage_error",
                "Не удалось сохранить ответ техкарты локально.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый файл техкарты.")
