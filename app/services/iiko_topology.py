"""Чтение XML по настроенному подключению и проверка связей по UUID."""

import asyncio
import hashlib
import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel

from app.core.config import BACKEND_DIR
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_topology import (
    CorporateGroupIdentity,
    CorporateGroupsResponse,
    CorporateHierarchyResponse,
    CorporateItem,
    DepartmentBinding,
    DepartmentMappingResponse,
    ReplicationResponse,
    ReplicationStatus,
)
from app.services.iiko_connections import IikoConnectionsService

logger = logging.getLogger(__name__)
MAX_TOPOLOGY_BYTES = 4 * 1024 * 1024


def _read_xml[T: BaseModel](path: Path, root_name: str, item_name: str, model: type[T]) -> list[T]:
    if path.stat().st_size > MAX_TOPOLOGY_BYTES:
        raise ValueError("XML exceeds size limit")
    root = ElementTree.parse(
        path, forbid_dtd=True, forbid_entities=True, forbid_external=True
    ).getroot()
    if root.tag != root_name:
        raise ValueError("Unexpected XML root")
    names = {field.validation_alias or name for name, field in model.model_fields.items()}
    rows = []
    for item in root:
        if item.tag != item_name:
            raise ValueError("Unexpected XML item")
        values = {}
        for field in item:
            if field.tag not in names:
                continue  # Дополнительные, в том числе вложенные юридические поля, остаются в RAW.
            if field.tag in values or len(field):
                raise ValueError("Repeated or nested scalar")
            value = field.text or ""
            nil = field.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in {"true", "1"}
            if nil and value:
                raise ValueError("Nil field contains text")
            if nil or (
                not value
                and field.tag in {"parentId", "departmentId", "lastReceiveDate", "lastSendDate"}
            ):
                value = None
            values[field.tag] = value
        rows.append(model.model_validate(values))
    return rows


def read_departments(path: Path) -> list[CorporateItem]:
    try:
        rows = _read_xml(path, "corporateItemDtoes", "corporateItemDto", CorporateItem)
        index = {row.id: row for row in rows}
        if len(index) != len(rows):
            raise ValueError("Duplicate department UUID")
        completed = set()
        for row in rows:
            branch = set()
            current = row.id
            while current in index and current not in completed:
                if current in branch:
                    raise ValueError("Cyclic corporate hierarchy")
                branch.add(current)
                current = index[current].parent_id
            completed.update(branch)
        return rows
    except (ParseError, DefusedXmlException, ValueError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_departments_invalid_response", "Некорректная XML-иерархия подразделений."
        ) from None


def read_replication(path: Path) -> list[ReplicationStatus]:
    try:
        rows = _read_xml(path, "replicationStatusDtoes", "replicationStatusDto", ReplicationStatus)
        if len({row.department_id for row in rows}) != len(rows):
            raise ValueError("Duplicate department UUID in replication")
        return rows
    except (ParseError, DefusedXmlException, ValueError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_replication_invalid_response", "iiko не вернул читаемые статусы репликации в XML."
        ) from None


def read_corporate_groups(path: Path) -> list[CorporateGroupIdentity]:
    try:
        rows = _read_xml(path, "groupDtoes", "groupDto", CorporateGroupIdentity)
        if len({row.id for row in rows}) != len(rows):
            raise ValueError("Duplicate group UUID")
        return rows
    except (ParseError, DefusedXmlException, ValueError, OverflowError, RecursionError):
        raise IikoError(
            "iiko_corporate_groups_invalid_response", "Некорректные XML-группы подразделений."
        ) from None


def map_departments(
    primary: CorporateHierarchyResponse,
    rms_ids: list[str],
    snapshots: dict[str, CorporateHierarchyResponse],
    groups: dict[str, CorporateGroupsResponse],
) -> DepartmentMappingResponse:
    chain = {row.id: row for row in primary.items if row.type == "DEPARTMENT"}
    bindings = []
    for connection_id in rms_ids:
        snapshot = snapshots.get(connection_id)
        binding = DepartmentBinding(connection_id=connection_id, state="not_loaded")
        if snapshot is not None:
            departments = {row.id: row for row in snapshot.items if row.type == "DEPARTMENT"}
            binding.rms_snapshot_id = snapshot.snapshot_id
            binding.rms_received_at = snapshot.received_at
            local_groups = groups.get(connection_id)
            if local_groups is None:
                binding.state = "groups_not_loaded"
            else:
                binding.groups_snapshot_id = local_groups.snapshot_id
                binding.groups_received_at = local_groups.received_at
                binding.candidate_ids = sorted(
                    {
                        row.department_id
                        for row in local_groups.items
                        if row.department_id is not None
                    }
                )
                if not departments:
                    binding.state = "missing_department"
                elif not local_groups.items or any(
                    row.department_id is None for row in local_groups.items
                ):
                    binding.state = "missing_group_department"
                elif len(binding.candidate_ids) > 1:
                    binding.state = "multiple_departments"
                else:
                    department_id = binding.candidate_ids[0]
                    binding.department_id = department_id
                    if department_id not in departments:
                        binding.state = "not_in_rms"
                    elif department_id not in chain:
                        binding.state = "not_in_chain"
                    else:
                        binding.state = "matched"
                        binding.rms_department_name = departments[department_id].name
                        binding.department_name = chain[department_id].name
        bindings.append(binding)
    counts = Counter(b.department_id for b in bindings if b.state == "matched")
    for binding in bindings:
        if binding.state == "matched" and counts[binding.department_id] > 1:
            binding.state = "duplicate_binding"
    matched = {b.department_id for b in bindings if b.state == "matched"}
    return DepartmentMappingResponse(
        primary_snapshot_id=primary.snapshot_id,
        primary_received_at=primary.received_at,
        bindings=bindings,
        unmapped_chain_departments=[row for key, row in chain.items() if key not in matched],
    )


class IikoTopologyService:
    def __init__(self, connections: IikoConnectionsService, directory: Path | None = None):
        self._connections = connections
        self._directory = directory if directory is not None else BACKEND_DIR / ".local/topology"
        self._hierarchies: dict[str, CorporateHierarchyResponse] = {}
        self._groups: dict[str, CorporateGroupsResponse] = {}

    async def _load(
        self, connection_id: str, resource: Literal["departments", "replication", "groups"]
    ) -> CorporateHierarchyResponse | ReplicationResponse | CorporateGroupsResponse:
        connection = self._connections.get_connection(connection_id)
        snapshot_id = uuid4()
        directory = self._directory / connection_id
        part = directory / f"{snapshot_id}.xml.part"
        raw = directory / f"{snapshot_id}.xml"
        meta = directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            downloader = {
                "departments": connection.auth.download_departments,
                "replication": connection.auth.download_replication_statuses,
                "groups": connection.auth.download_corporate_groups,
            }[resource]
            download = await downloader(part)
            received_at = datetime.now(UTC)
            metadata = {
                "connection_id": connection_id,
                "snapshot_id": snapshot_id,
                "received_at": received_at,
                "source_bytes": download.size_bytes,
                "sha256": download.sha256,
            }
            if resource == "departments":
                rows = await asyncio.to_thread(read_departments, part)
                ids = {row.id for row in rows}
                response = CorporateHierarchyResponse(
                    **metadata,
                    total=len(rows),
                    items=rows,
                    missing_parent_ids=sorted(
                        {
                            row.parent_id
                            for row in rows
                            if row.parent_id is not None and row.parent_id not in ids
                        }
                    ),
                )
            elif resource == "groups":
                groups = await asyncio.to_thread(read_corporate_groups, part)
                response = CorporateGroupsResponse(**metadata, total=len(groups), items=groups)
            else:
                statuses = await asyncio.to_thread(read_replication, part)
                response = ReplicationResponse(
                    **metadata,
                    total=len(statuses),
                    items=statuses,
                    status_counts=dict(Counter(row.status for row in statuses)),
                )
            metadata = response.model_dump(mode="json", exclude={"items"})
            identity = (
                f"{str(connection.settings.iiko_base_url).rstrip('/')}\n"
                f"{connection.settings.iiko_login}"
            )
            metadata["source_fingerprint"] = hashlib.sha256(identity.encode()).hexdigest()
            metadata["source_endpoint"] = {
                "departments": "corporation/departments",
                "groups": "corporation/groups",
                "replication": "replication/statuses",
            }[resource]
            metadata["source_accept"] = "application/xml"
            descriptor = os.open(meta, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as out:
                json.dump(metadata, out, ensure_ascii=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            part.replace(raw)
            saved = True
            return response
        except OSError:
            raise IikoError(
                "iiko_topology_storage_error",
                "Не удалось сохранить данные подразделений/репликации.",
                status_code=500,
            ) from None
        finally:
            for path in [part] if saved else [part, meta]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённый файл топологии iiko.")

    async def get_departments(self, connection_id: str) -> CorporateHierarchyResponse:
        response = cast(CorporateHierarchyResponse, await self._load(connection_id, "departments"))
        previous = self._hierarchies.get(connection_id)
        if previous is None or response.received_at >= previous.received_at:
            self._hierarchies[connection_id] = response
        return response

    async def get_replication(self) -> ReplicationResponse:
        return cast(ReplicationResponse, await self._load("primary", "replication"))

    async def get_corporate_groups(self, connection_id: str) -> CorporateGroupsResponse:
        response = cast(CorporateGroupsResponse, await self._load(connection_id, "groups"))
        previous = self._groups.get(connection_id)
        if previous is None or response.received_at >= previous.received_at:
            self._groups[connection_id] = response
        return response

    def mapping(self) -> DepartmentMappingResponse:
        primary = self._hierarchies.get("primary")
        if primary is None:
            raise IikoError(
                "iiko_departments_not_loaded",
                "Сначала загрузите иерархию primary.",
                status_code=409,
            )
        return map_departments(
            primary,
            [key for key in self._connections.connection_ids() if key != "primary"],
            self._hierarchies,
            self._groups,
        )
