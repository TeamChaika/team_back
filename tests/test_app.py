from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.core.config import Settings
from app.main import create_app


def test_app_serves_health_and_info() -> None:
    application = create_app(Settings(app_name="Chaika Test", _env_file=None))

    with TestClient(application) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        info = client.get("/api/v1/info")
        assert info.status_code == 200
        assert info.json() == {"name": "Chaika Test", "version": "0.1.0"}
        assert client.get("/api/v1/missing").status_code == 404


def test_swagger_and_openapi_expose_current_routes() -> None:
    with TestClient(create_app(Settings(_env_file=None))) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "swagger-ui" in docs.text

        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert set(schema["paths"]) == {
            "/api/v1/sync/discount-details",
            "/api/v1/sync/accounts",
            "/api/v1/sync/cash-shifts",
            "/api/v1/iiko/employee-roles",
            "/api/v1/iiko/employee-roles/load",
            "/api/v1/sync/employee-roles",
            "/api/v1/sync/events",
            "/api/v1/events/orders/topology",
            "/api/v1/iiko/connections/{source_id}/events",
            "/api/v1/health",
            "/api/v1/info",
            "/api/v1/sync/refresh",
            "/api/v1/sync/employees",
            "/api/v1/sync/store-balances",
            "/api/v1/sync/counteragent-balances",
            "/api/v1/sync/outgoing-invoices",
            "/api/v1/iiko/outgoing-invoices",
            "/api/v1/iiko/transfers",
            "/api/v1/sync/transfers",
            "/api/v1/sync/dictionaries",
            "/api/v1/iiko/dictionaries/{kind}",
            "/api/v1/iiko/dictionaries/{kind}/load",
            "/api/v1/sync/jobs/{job_id}",
            "/api/v1/iiko/auth",
            "/api/v1/iiko/logout",
            "/api/v1/iiko/status",
            "/api/v1/iiko/products",
            "/api/v1/iiko/products/load",
            "/api/v1/iiko/groups",
            "/api/v1/iiko/groups/load",
            "/api/v1/iiko/assembly-charts/assembled",
            "/api/v1/iiko/assembly-charts/all",
            "/api/v1/iiko/incoming-invoices",
            "/api/v1/iiko/stores",
            "/api/v1/iiko/stores/load",
            "/api/v1/iiko/reports/counteragent-balances",
            "/api/v1/iiko/reports/store-balances",
            "/api/v1/iiko/employees",
            "/api/v1/iiko/employees/load",
            "/api/v1/iiko/writeoffs",
            "/api/v1/iiko/cash-shifts",
            "/api/v1/iiko/connections",
            "/api/v1/iiko/connections/{connection_id}/server-type",
            "/api/v1/iiko/connections/{connection_id}/logout",
            "/api/v1/iiko/olap/columns",
            "/api/v1/iiko/olap/sales/daily",
            "/api/v1/iiko/replication/statuses",
            "/api/v1/iiko/connections/{connection_id}/departments",
            "/api/v1/iiko/connections/{connection_id}/corporate-groups",
            "/api/v1/iiko/connections/department-mapping",
        }
        for route, path in schema["paths"].items():
            operation = path.get("get", path.get("post"))
            response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
            if route == "/api/v1/sync/discount-details":
                # Orders and order items have separate shapes; dedicated tests cover both.
                assert response_schema == {}
            else:
                assert response_schema


def test_settings_use_chaika_environment_prefix(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "Unrelated project")
    monkeypatch.delenv("CHAIKA_APP_NAME", raising=False)
    assert Settings(_env_file=None).app_name == "Chaika Team API"

    monkeypatch.setenv("CHAIKA_APP_NAME", "Chaika Local")
    assert Settings(_env_file=None).app_name == "Chaika Local"


def test_app_instances_keep_their_own_settings() -> None:
    first = create_app(Settings(app_name="First", _env_file=None))
    second = create_app(Settings(app_name="Second", _env_file=None))

    with TestClient(first) as first_client, TestClient(second) as second_client:
        assert first_client.get("/api/v1/info").json()["name"] == "First"
        assert second_client.get("/api/v1/info").json()["name"] == "Second"
