"""Authenticate the explicitly configured edge before consuming its client IP."""

import hmac
from ipaddress import ip_address
from pathlib import Path

from .repository import Problem


class EdgePeer:
    def __init__(self, secret_file, trusted_peers=()):
        path = Path(secret_file)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("Edge capability must be a private regular file")
        self.secret = path.read_text().strip()
        if (
            len(self.secret) < 32
            or not self.secret.isascii()
            or any(c.isspace() for c in self.secret)
        ):
            raise ValueError("Invalid edge capability")
        self.trusted = frozenset(str(ip_address(peer)) for peer in trusted_peers)

    def resolve(self, request):
        peer = request.client.host if request.client else None
        trusted_transport = peer is None or peer in self.trusted
        if not trusted_transport:
            return peer  # An external client cannot opt in to edge identity.
        tokens = request.headers.getlist("x-restcontrol-proxy-token")
        clients = request.headers.getlist("x-restcontrol-client-ip")
        if (
            len(tokens) != 1
            or not tokens[0].isascii()
            or not hmac.compare_digest(tokens[0], self.secret)
            or len(clients) != 1
        ):
            raise Problem(403, "invalid_edge_identity", "Источник запроса не подтверждён")
        try:
            return str(ip_address(clients[0]))
        except ValueError:
            raise Problem(403, "invalid_edge_identity", "Источник запроса не подтверждён") from None
