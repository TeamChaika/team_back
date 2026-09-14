"""Публикация и чтение проверенных локальных снимков справочников."""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel

from app.core.config import Settings
from app.integrations.iiko.client import CatalogDownload
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_catalog import IikoSnapshot

logger = logging.getLogger(__name__)


class LocalCatalog[ItemT: BaseModel, SnapshotT: IikoSnapshot]:
    def __init__(
        self,
        settings: Settings,
        *,
        name: str,
        directory: Path,
        max_bytes: int,
        download: Callable[[Path], Awaitable[CatalogDownload]],
        read: Callable[[Path, int], list[ItemT]],
        snapshot_model: type[SnapshotT],
        summarize: Callable[[list[ItemT]], dict],
        raw_format: Literal["json", "xml"] = "json",
    ) -> None:
        self._directory = directory
        self._max_bytes = max_bytes
        self._name = name
        self._download = download
        self._read = read
        self._snapshot_model = snapshot_model
        self._summarize = summarize
        self._raw_format = raw_format
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        self._source_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._lock = asyncio.Lock()
        self._snapshot: SnapshotT | None = None
        self._items: list[ItemT] = []

    def _error(self, code: str, message: str, status: int = 500) -> IikoError:
        return IikoError(f"iiko_{self._name}_{code}", message, status_code=status)

    def _publish(
        self,
        snapshot_id: UUID,
        part_file: Path,
        download: CatalogDownload,
        received_at: datetime,
        items: list[ItemT],
    ) -> SnapshotT:
        snapshot = self._snapshot_model(
            snapshot_id=snapshot_id,
            received_at=received_at,
            total=len(items),
            source_bytes=download.size_bytes,
            sha256=download.sha256,
            **self._summarize(items),
        )
        metadata = {
            "source_fingerprint": self._source_fingerprint,
            "snapshot": snapshot.model_dump(mode="json"),
        }
        pointer = self._directory / f"{snapshot_id}.meta.part"
        raw_file = self._directory / f"{snapshot_id}.{self._raw_format}"
        try:
            descriptor = os.open(pointer, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(metadata, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            part_file.replace(raw_file)
            # Только полностью загруженный и проверенный файл становится текущим.
            pointer.replace(self._directory / "current.json")
        finally:
            pointer.unlink(missing_ok=True)
        self._snapshot = snapshot
        self._items = items
        return snapshot

    async def load(self) -> SnapshotT:
        async with self._lock:
            snapshot_id = uuid4()
            part_file = self._directory / f"{snapshot_id}.{self._raw_format}.part"
            try:
                self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._directory.chmod(0o700)
                download = await self._download(part_file)
                received_at = datetime.now(UTC)
                items = await asyncio.to_thread(self._read, part_file, self._max_bytes)
                return self._publish(snapshot_id, part_file, download, received_at, items)
            except OSError:
                raise self._error(
                    "storage_error",
                    "Не удалось сохранить справочник локально. Проверьте место и права.",
                ) from None
            finally:
                try:
                    part_file.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённую локальную выгрузку.")

    def _restore(self) -> tuple[SnapshotT, list[ItemT]]:
        pointer = self._directory / "current.json"
        try:
            if not pointer.exists():
                raise self._error(
                    "not_loaded",
                    f"Сначала загрузите справочник через POST /api/v1/iiko/{self._name}/load.",
                    409,
                )
            metadata = json.loads(pointer.read_text(encoding="utf-8"))
            if metadata["source_fingerprint"] != self._source_fingerprint:
                raise self._error(
                    "source_changed",
                    "Сохранённый справочник относится к другому подключению. Загрузите новый.",
                    409,
                )
            snapshot = self._snapshot_model.model_validate(metadata["snapshot"])
            raw_file = self._directory / f"{snapshot.snapshot_id}.{self._raw_format}"
            if raw_file.stat().st_size != snapshot.source_bytes:
                raise ValueError("Snapshot size mismatch")
            items = self._read(raw_file, self._max_bytes)
            with raw_file.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if len(items) != snapshot.total or digest != snapshot.sha256:
                raise ValueError("Snapshot metadata mismatch")
            return snapshot, items
        except (OSError, ValueError, KeyError, TypeError, IikoError) as exc:
            if isinstance(exc, IikoError) and exc.status_code == 409:
                raise
            raise self._error(
                "storage_error",
                "Сохранённый справочник недоступен или повреждён. Загрузите его заново.",
            ) from None

    async def page(
        self, offset: int, limit: int, snapshot_id: UUID | None = None
    ) -> tuple[SnapshotT, list[ItemT]]:
        async with self._lock:
            if self._snapshot is None:
                self._snapshot, self._items = await asyncio.to_thread(self._restore)
            if snapshot_id is not None and snapshot_id != self._snapshot.snapshot_id:
                raise self._error(
                    "snapshot_changed",
                    "Справочник обновился. Начните просмотр нового снимка с первой страницы.",
                    409,
                )
            return self._snapshot, self._items[offset : offset + limit]
