"""Чтение групп и проверка связей parent без рекурсивного построения дерева."""

from pathlib import Path
from uuid import UUID

import ijson
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_groups import IikoGroup, IikoGroupsPage, IikoGroupsSnapshot
from app.services.iiko_auth import IikoAuthService
from app.services.local_catalog import LocalCatalog


def read_groups(path: Path, max_bytes: int) -> list[IikoGroup]:
    if path.stat().st_size > max_bytes:
        raise IikoError("iiko_groups_too_large", "Файл групп превышает лимит размера.")
    groups: list[IikoGroup] = []
    ids: set[UUID] = set()
    try:
        with path.open("rb") as source:
            events = ijson.parse(source)
            if next(events, None) != ("", "start_array", None):
                raise ValueError("Expected a JSON array")
            for item in ijson.items(events, "item"):
                group = IikoGroup.model_validate(item)
                if group.id in ids or group.deleted:
                    raise ValueError("Duplicate ID or unexpected deleted group")
                ids.add(group.id)
                groups.append(group)
    except (ijson.JSONError, ValidationError, ValueError, OverflowError):
        raise IikoError(
            "iiko_groups_invalid_response",
            "Ответ групп повреждён или не соответствует ожидаемой структуре.",
        ) from None
    return groups


def summarize_groups(groups: list[IikoGroup]) -> dict:
    parents = {group.id: group.parent_id for group in groups}
    visited: set[UUID] = set()
    cycles: set[UUID] = set()
    for group_id in parents:
        current: UUID | None = group_id
        chain: dict[UUID, int] = {}
        while current in parents and current not in visited:
            if current in chain:
                cycles.update(list(chain)[chain[current] :])
                break
            chain[current] = len(chain)
            current = parents[current]
        visited.update(chain)
    return {
        "root_groups": sum(group.parent_id is None for group in groups),
        "missing_parent_ids": sorted(
            {parent for parent in parents.values() if parent is not None and parent not in parents}
        ),
        "cycle_group_ids": sorted(cycles),
    }


class IikoGroupsService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._catalog = LocalCatalog(
            settings,
            name="groups",
            directory=directory if directory is not None else BACKEND_DIR / ".local/groups",
            max_bytes=settings.iiko_groups_max_response_bytes,
            download=auth.download_groups,
            read=read_groups,
            snapshot_model=IikoGroupsSnapshot,
            summarize=summarize_groups,
        )

    async def load(self) -> IikoGroupsSnapshot:
        return await self._catalog.load()

    async def page(
        self, offset: int, limit: int, snapshot_id: UUID | None = None
    ) -> IikoGroupsPage:
        snapshot, items = await self._catalog.page(offset, limit, snapshot_id)
        return IikoGroupsPage(snapshot=snapshot, offset=offset, limit=limit, items=items)
