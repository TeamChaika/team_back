"""Explicit origin contract for local development and a dedicated TLS reverse proxy."""

import ipaddress
import re
from urllib.parse import urlsplit


def validate_origin(origin, mode):
    if mode not in ("local", "production"):
        raise ValueError("Unsupported runtime mode")
    parsed = urlsplit(origin)
    if (
        not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Origin must contain only scheme and authority")
    if mode == "local":
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("Local origin must be an explicit localhost HTTP origin")
    else:
        hostname = parsed.hostname
        if (
            parsed.scheme != "https"
            or parsed.port is not None
            or origin != "https://" + hostname
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname)
            or "." not in hostname
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in hostname.split(".")
            )
        ):
            raise ValueError("Production requires a canonical HTTPS DNS origin without port")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("Production requires a DNS hostname")
        if hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError("Production requires a public DNS hostname")
    return parsed.netloc
