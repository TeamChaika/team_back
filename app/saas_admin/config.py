"""Explicit origin contract for local development and a dedicated TLS reverse proxy."""

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SupabaseSettings(BaseSettings):
    """Explicit credentials for the existing Supabase; no dashboard settings import."""

    model_config = SettingsConfigDict(
        env_prefix="CHAIKA_SAAS_", extra="ignore", hide_input_in_errors=True
    )
    database_url: SecretStr
    supabase_url: str
    anon_key: SecretStr
    auth_admin_key: SecretStr

    @field_validator("supabase_url")
    @classmethod
    def auth_origin(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or (parsed.username or parsed.password or parsed.query or parsed.fragment)
        ):
            raise ValueError("Supabase URL must be a trusted HTTP(S) base URL")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("Remote Supabase requires HTTPS")
        return value.rstrip("/")


def validate_private_key(directory):
    directory = Path(directory)
    key = directory / "credentials.key"
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError("A private existing key directory is required")
    if key.is_symlink() or not key.is_file() or key.stat().st_mode & 0o077:
        raise ValueError("Restore the original private credentials.key")


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
