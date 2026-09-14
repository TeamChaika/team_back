import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.iiko_topology import (
    CorporateGroupIdentity,
    CorporateGroupsResponse,
    CorporateHierarchyResponse,
    CorporateItem,
)
from app.services.iiko_topology import map_departments

CONNECTIONS = "/api/v1/iiko/connections"
REPLICATION = "/api/v1/iiko/replication/statuses"
MAPPING = f"{CONNECTIONS}/department-mapping"


def node(number=1, parent=None, kind="DEPARTMENT", name="Ресторан"):
    return (
        f"<corporateItemDto><id>{UUID(int=number)}</id>"
        + (f"<parentId>{UUID(int=parent)}</parentId>" if parent else "")
        + f"<name>{name}</name><type>{kind}</type><code>001</code>"
        "<jurPersonAdditionalPropertiesDto><bank>private-value</bank>"
        "</jurPersonAdditionalPropertiesDto></corporateItemDto>"
    )


def hierarchy(*nodes):
    return ("<corporateItemDtoes>" + "".join(nodes) + "</corporateItemDtoes>").encode()


def group(number=1, department=1):
    return (
        f"<groupDto><id>{UUID(int=number)}</id><name>Группа</name>"
        + (f"<departmentId>{UUID(int=department)}</departmentId>" if department else "")
        + "<groupServiceMode>TABLE_SERVICE</groupServiceMode>"
        "<pointOfSaleDtoes><pointOfSaleDto><name>RAW only</name>"
        "</pointOfSaleDto></pointOfSaleDtoes>"
        "</groupDto>"
    )


def groups(*items):
    return ("<groupDtoes>" + "".join(items) + "</groupDtoes>").encode()


def status(number=1, value="SUCCESS", dates=True):
    return (
        f"<replicationStatusDto><departmentId>{UUID(int=number)}</departmentId>"
        f"<departmentName>Ресторан</departmentName><status>{value}</status>"
        + (
            "<lastReceiveDate>2026-09-10T18:39:04.606+03:00</lastReceiveDate>"
            "<lastSendDate>2026-09-10T18:39:03.733+03:00</lastSendDate>"
            if dates
            else ""
        )
        + "</replicationStatusDto>"
    )


def statuses(*items):
    return ("<replicationStatusDtoes>" + "".join(items) + "</replicationStatusDtoes>").encode()


def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "connections": [
                    {
                        "id": "rms-one",
                        "label": "RMS",
                        "base_url": "https://rms.iiko.example/resto/api",
                        "use_primary_credentials": True,
                    }
                ]
            }
        )
    )
    return Settings(
        _env_file=None,
        iiko_base_url="https://chain.iiko.example/resto/api",
        iiko_login="test-login",
        iiko_password="test-password",
        iiko_connections_file=path,
    )


class Source:
    def __init__(self):
        self.requests = []
        self.departments = {
            "chain.iiko.example": hierarchy(node(), node(2, kind="CENTRALSTORE")),
            "rms.iiko.example": hierarchy(node(name="Другое имя RMS")),
        }
        self.groups = groups(group())
        self.replication = statuses(status(), status(2, "FUTURE_STATUS", dates=False))
        self.codes = iter([200] * 100)

    def __call__(self, request):
        self.requests.append(request)
        assert request.method == "GET"
        token = f"token-{request.url.host}"
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=token)
        assert request.headers["cookie"] == f"key={token}"
        assert request.headers["accept"] == "application/xml"
        if request.url.path.endswith("/departments"):
            assert dict(request.url.params) == {"revisionFrom": "-1"}
            return httpx.Response(next(self.codes), content=self.departments[request.url.host])
        if request.url.path.endswith("/groups"):
            assert dict(request.url.params) == {"revisionFrom": "-1"}
            return httpx.Response(next(self.codes), content=self.groups)
        assert request.url.path == "/resto/api/replication/statuses"
        assert request.url.host == "chain.iiko.example" and not request.url.query
        return httpx.Response(next(self.codes), content=self.replication)


def application(tmp_path, source):
    return create_app(
        config(tmp_path),
        iiko_transport=httpx.MockTransport(source),
        topology_directory=tmp_path / "topology",
    )


def test_named_hierarchies_mapping_and_snapshot_evidence(tmp_path):
    source = Source()
    with TestClient(application(tmp_path, source)) as c:
        assert c.get(MAPPING).status_code == 409 and not source.requests
        chain = c.get(f"{CONNECTIONS}/primary/departments").json()
        assert chain["total"] == 2 and chain["connection_id"] == "primary"
        assert c.get(MAPPING).json()["bindings"][0]["state"] == "not_loaded"
        rms = c.get(f"{CONNECTIONS}/rms-one/departments").json()
        assert c.get(MAPPING).json()["bindings"][0]["state"] == "groups_not_loaded"
        local_groups = c.get(f"{CONNECTIONS}/rms-one/corporate-groups").json()
        assert "RAW only" not in json.dumps(local_groups)
        calls = len(source.requests)
        mapping = c.get(MAPPING).json()
        assert len(source.requests) == calls
        binding = mapping["bindings"][0]
        assert binding["state"] == "matched" and binding["department_id"] == str(UUID(int=1))
        assert (
            binding["department_name"] == "Ресторан"
            and binding["rms_department_name"] == "Другое имя RMS"
        )
        assert binding["rms_snapshot_id"] == rms["snapshot_id"]
        assert binding["groups_snapshot_id"] == local_groups["snapshot_id"]
        raw_groups = tmp_path / "topology/rms-one" / f"{local_groups['snapshot_id']}.xml"
        assert raw_groups.read_bytes() == source.groups
        assert local_groups["sha256"] == hashlib.sha256(raw_groups.read_bytes()).hexdigest()
        assert mapping["primary_snapshot_id"] == chain["snapshot_id"]
        assert mapping["unmapped_chain_departments"] == []
        for data, host in [(chain, "chain.iiko.example"), (rms, "rms.iiko.example")]:
            folder = tmp_path / "topology" / data["connection_id"]
            raw = folder / f"{data['snapshot_id']}.xml"
            meta = folder / f"{data['snapshot_id']}.meta.json"
            assert raw.read_bytes() == source.departments[host]
            assert data["sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
            assert data["source_bytes"] == raw.stat().st_size
            metadata = json.loads(meta.read_bytes())
            assert (
                metadata["source_fingerprint"]
                == hashlib.sha256(f"https://{host}/resto/api\ntest-login".encode()).hexdigest()
            )
            assert metadata["source_accept"] == "application/xml"
            assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
            assert folder.stat().st_mode & 0o777 == 0o700
            assert "private-value" not in json.dumps(data)
        assert c.post(f"{CONNECTIONS}/rms-one/logout").json()["state"] == "logged_out"
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_replication_xml_keeps_dates_unknown_status_and_missing_dates(tmp_path):
    source = Source()
    with TestClient(application(tmp_path, source)) as c:
        r = c.get(REPLICATION)
        assert r.status_code == 200
        data = r.json()
        assert data["status_counts"] == {"SUCCESS": 1, "FUTURE_STATUS": 1}
        assert data["items"][0]["last_receive_date"] == "2026-09-10T18:39:04.606000+03:00"
        assert data["items"][1]["last_receive_date"] is None
        assert all(r.url.host == "chain.iiko.example" for r in source.requests)
        source.replication = statuses()
        empty = c.get(REPLICATION).json()
        assert empty["items"] == [] and empty["status_counts"] == {}
        assert "healthy" not in empty


@pytest.mark.parametrize(
    "payload",
    [
        b"[{},{}]",
        b"<bad/>",
        statuses("<replicationStatusDto/>"),
        statuses(status(), status()),
        statuses(status().replace("SUCCESS", "")),
        statuses(status().replace("+03:00", "")),
        statuses(status().replace("2026-09-10T18:39:04.606+03:00", "1728000000")),
        statuses(
            status().replace(
                "<status>SUCCESS</status>", "<status>SUCCESS</status><status>MISSED</status>"
            )
        ),
        b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><replicationStatusDtoes>&e;</replicationStatusDtoes>',
    ],
)
def test_bad_replication_never_becomes_success(tmp_path, payload):
    source = Source()
    source.replication = payload
    with TestClient(application(tmp_path, source)) as c:
        r = c.get(REPLICATION)
        assert (
            r.status_code == 502
            and r.json()["error"]["code"] == "iiko_replication_invalid_response"
        )
        assert not list((tmp_path / "topology").rglob("*.xml"))
        assert not list((tmp_path / "topology").rglob("*.part"))


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        hierarchy("<wrong/>"),
        hierarchy(node(), node()),
        hierarchy(node(parent=1)),
        hierarchy(node(parent=2), node(2, parent=1)),
        hierarchy(node().replace("<type>DEPARTMENT</type>", "")),
        hierarchy(node().replace("<code>001</code>", "<code><nested/></code>")),
        hierarchy(node().replace("<code>001</code>", "<code>001</code><code>002</code>")),
        b'<!DOCTYPE x [<!ENTITY e "private-error">]><corporateItemDtoes>&e;</corporateItemDtoes>',
    ],
)
def test_bad_hierarchy_keeps_previous_observation(tmp_path, payload):
    source = Source()
    with TestClient(application(tmp_path, source)) as c:
        previous = c.get(f"{CONNECTIONS}/primary/departments").json()
        source.departments["chain.iiko.example"] = payload
        r = c.get(f"{CONNECTIONS}/primary/departments")
        assert r.status_code == 502 and "private-error" not in r.text
        assert c.get(MAPPING).json()["primary_snapshot_id"] == previous["snapshot_id"]


def test_missing_parents_unknown_types_empty_hierarchy_and_restart(tmp_path):
    source = Source()
    source.departments["chain.iiko.example"] = hierarchy(node(parent=99, kind="FUTURE_TYPE"))
    with TestClient(application(tmp_path, source)) as c:
        data = c.get(f"{CONNECTIONS}/primary/departments").json()
        assert data["missing_parent_ids"] == [str(UUID(int=99))]
        assert data["items"][0]["type"] == "FUTURE_TYPE"
        source.departments["chain.iiko.example"] = hierarchy()
        assert c.get(f"{CONNECTIONS}/primary/departments").json()["total"] == 0
    calls = len(source.requests)
    with TestClient(application(tmp_path, source)) as c:
        assert c.get(MAPPING).status_code == 409 and len(source.requests) == calls


def snapshot(connection_id, items):
    return CorporateHierarchyResponse(
        snapshot_id=uuid4(),
        received_at=datetime.now(UTC),
        total=len(items),
        source_bytes=0,
        sha256="0" * 64,
        connection_id=connection_id,
        items=items,
        missing_parent_ids=[],
    )


def group_snapshot(connection_id, departments):
    return CorporateGroupsResponse(
        snapshot_id=uuid4(),
        received_at=datetime.now(UTC),
        total=len(departments),
        source_bytes=0,
        sha256="0" * 64,
        connection_id=connection_id,
        items=[
            CorporateGroupIdentity(
                id=uuid4(), name="Group", departmentId=department, groupServiceMode="TABLE_SERVICE"
            )
            for department in departments
        ],
    )


def test_mapping_only_uses_unique_department_uuid_and_marks_all_conflicts():
    first = CorporateItem(id=UUID(int=1), name="Same", type="DEPARTMENT")
    second = CorporateItem(id=UUID(int=2), name="Same", type="DEPARTMENT")
    outside = CorporateItem(id=UUID(int=3), name="Same", type="DEPARTMENT")
    primary = snapshot("primary", [first, second])
    snapshots = {
        "one": snapshot("one", [first]),
        "duplicate": snapshot("duplicate", [first]),
        "outside": snapshot("outside", [outside]),
        "empty": snapshot("empty", []),
        "many": snapshot("many", [first, second]),
    }
    local_groups = {
        key: group_snapshot(key, [row.id for row in snap.items]) for key, snap in snapshots.items()
    }
    result = map_departments(primary, [*snapshots, "not-loaded"], snapshots, local_groups)
    assert {b.connection_id: b.state for b in result.bindings} == {
        "one": "duplicate_binding",
        "duplicate": "duplicate_binding",
        "outside": "not_in_chain",
        "empty": "missing_department",
        "many": "multiple_departments",
        "not-loaded": "not_loaded",
    }
    assert {r.id for r in result.unmapped_chain_departments} == {first.id, second.id}


@pytest.mark.parametrize("resource", ["replication", "departments", "corporate-groups"])
@pytest.mark.parametrize("codes", [[401, 200], [401, 401], [403], [500]])
def test_only_401_is_retried_once(tmp_path, resource, codes):
    source = Source()
    source.codes = iter(codes)
    url = REPLICATION if resource == "replication" else f"{CONNECTIONS}/primary/{resource}"
    with TestClient(application(tmp_path, source)) as c:
        assert c.get(url).status_code == (200 if codes[-1] == 200 else 502)
        assert sum(
            r.url.path.endswith(("/statuses", "/departments", "/groups")) for r in source.requests
        ) == len(codes)


def test_unknown_connection_is_rejected_before_network_or_path_write(tmp_path):
    source = Source()
    with TestClient(application(tmp_path, source)) as c:
        assert c.get(f"{CONNECTIONS}/unknown/departments").status_code == 404
        assert c.get(f"{CONNECTIONS}/rms-one/replication/statuses").status_code == 404
        assert not source.requests


def test_disk_failure_is_safe_and_cleans_partial_files(tmp_path, monkeypatch):
    def broken_replace(path, target):
        raise PermissionError("private-error")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(application(tmp_path, Source())) as c:
        r = c.get(REPLICATION)
        assert r.status_code == 500 and "private-error" not in r.text
        assert not list((tmp_path / "topology").rglob("*.part"))
        assert not list((tmp_path / "topology").rglob("*.meta.json"))


def test_older_slow_completion_does_not_replace_newer_hierarchy(tmp_path, monkeypatch):
    async def scenario():
        app = application(tmp_path, Source())
        async with app.router.lifespan_context(app):
            service = app.state.iiko_topology
            old, new = snapshot("primary", []), snapshot("primary", [])
            old = old.model_copy(update={"received_at": new.received_at - timedelta(seconds=1)})
            responses = iter([new, old])

            async def load(*args):
                return next(responses)

            monkeypatch.setattr(service, "_load", load)
            await service.get_departments("primary")
            await service.get_departments("primary")
            assert service.mapping().primary_snapshot_id == new.snapshot_id

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "references, expected",
    [
        ([1], "matched"),
        ([1, 1], "matched"),
        ([1, 2], "multiple_departments"),
        ([], "missing_group_department"),
        ([None], "missing_group_department"),
        ([1, None], "missing_group_department"),
        ([3], "not_in_rms"),
    ],
)
def test_shared_hierarchy_uses_all_local_group_references(references, expected):
    items = [CorporateItem(id=UUID(int=n), name="Same", type="DEPARTMENT") for n in (1, 2)]
    primary, rms = snapshot("primary", items), snapshot("rms", items)
    local = group_snapshot("rms", [UUID(int=n) if n else None for n in references])
    result = map_departments(primary, ["rms"], {"rms": rms}, {"rms": local})
    assert result.bindings[0].state == expected
    if expected == "matched":
        assert result.bindings[0].department_id == UUID(int=1)
        assert result.bindings[0].groups_snapshot_id == local.snapshot_id
        assert [row.id for row in result.unmapped_chain_departments] == [UUID(int=2)]


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"<wrong/>",
        groups(group(), group()),
        groups("<groupDto/>"),
        groups(group().replace("<departmentId>", "<departmentId>bad")),
        groups(group().replace("<name>Группа</name>", "<name><nested/></name>")),
        b'<!DOCTYPE x [<!ENTITY e "private-error">]><groupDtoes>&e;</groupDtoes>',
    ],
)
def test_bad_groups_preserve_previous_observation(tmp_path, payload):
    source = Source()
    with TestClient(application(tmp_path, source)) as c:
        c.get(f"{CONNECTIONS}/primary/departments").raise_for_status()
        c.get(f"{CONNECTIONS}/rms-one/departments").raise_for_status()
        previous = c.get(f"{CONNECTIONS}/rms-one/corporate-groups").json()
        source.groups = payload
        r = c.get(f"{CONNECTIONS}/rms-one/corporate-groups")
        assert r.status_code == 502 and "private-error" not in r.text
        assert c.get(MAPPING).json()["bindings"][0]["groups_snapshot_id"] == previous["snapshot_id"]
