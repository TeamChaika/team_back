import pytest
from starlette.requests import Request

from app.saas_admin.edge_peer import EdgePeer
from app.saas_admin.repository import Problem


def request(peer=None, **headers):
    return Request(
        {
            "type": "http",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "client": (peer, 1234) if peer else None,
        }
    )


def test_edge_authenticates_uds_ip_and_rejects_spoof(tmp_path):
    key = tmp_path / "edge.key"
    key.write_text("a" * 48)
    key.chmod(0o600)
    edge = EdgePeer(key, ["127.0.0.1"])
    headers = {"x-restcontrol-proxy-token": "a" * 48, "x-restcontrol-client-ip": "192.0.2.10"}
    assert edge.resolve(request(**headers)) == "192.0.2.10"
    assert edge.resolve(request("127.0.0.1", **headers)) == "192.0.2.10"
    assert edge.resolve(request("192.0.2.20", **headers)) == "192.0.2.20"
    with pytest.raises(Problem):
        edge.resolve(request(**{**headers, "x-restcontrol-proxy-token": "forged"}))
    with pytest.raises(Problem):
        edge.resolve(request(**{**headers, "x-restcontrol-client-ip": "192.0.2.10,192.0.2.11"}))
    with pytest.raises(Problem):
        edge.resolve(request(**{"x-forwarded-for": "192.0.2.10"}))
