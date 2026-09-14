"""Deployment contracts: assigned port and public ASGI entry point."""

import pytest

from app import serve


@pytest.mark.parametrize("value,expected", [(None, 8000), ("", 8000), ("9001", 9001)])
def test_start_uses_platform_port_and_public_api(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PORT", raising=False)
    else:
        monkeypatch.setenv("PORT", value)
    calls = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    serve.main()
    assert calls == [
        (
            "app.portal:app",
            {"host": "0.0.0.0", "port": expected, "workers": 1, "access_log": False},
        )
    ]


@pytest.mark.parametrize("value", ["0", "65536", "-1", "not-a-port", "${PORT:-8000}"])
def test_invalid_port_stops_with_actionable_message(monkeypatch, value):
    monkeypatch.setenv("PORT", value)
    with pytest.raises(SystemExit, match="PORT must be an integer"):
        serve.main()


def test_timeweb_default_import_exposes_portal_only():
    from main import app

    paths = {route.path for route in app.routes}
    assert {"/api/health", "/api/me", "/api/auth/login"} <= paths
    assert "/api/v1/sync/invoices" not in paths
    assert "/api/v1/iiko/auth" not in paths
