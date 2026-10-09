"""Local process IO destinations, isolated by immutable company configuration."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app.tenancy.config import load_runtime


def runtime_directory(name: str, legacy: Path) -> Path:
    runtime = load_runtime()
    return runtime.runtime_path(name) if runtime.mode == "tenant" else legacy


@contextmanager
def temporary_directory(*, prefix: str):
    runtime = load_runtime()
    directory = None
    if runtime.mode == "tenant":
        directory = runtime.runtime_path("temporary")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=prefix, dir=directory) as path:
        yield path


def collector_post(path: str, **kwargs):
    """POST only to this process's configured loopback collector or Unix socket."""
    if not path.startswith("/api/") or ".." in path:
        raise ValueError("Invalid private collector API path")
    runtime = load_runtime()
    kwargs["trust_env"] = False
    if runtime.mode == "legacy":
        return httpx.post("http://127.0.0.1:8010" + path, **kwargs)
    if runtime.collector_port is not None:
        return httpx.post(f"http://127.0.0.1:{runtime.collector_port}" + path, **kwargs)
    trust_env = kwargs.pop("trust_env")
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(runtime.collector_socket)), trust_env=trust_env
    ) as client:
        return client.post("http://localhost" + path, **kwargs)


def collector_url() -> str:
    runtime = load_runtime()
    if runtime.mode == "legacy":
        return "http://127.0.0.1:8010"
    return (
        f"http://127.0.0.1:{runtime.collector_port}"
        if runtime.collector_port
        else "http://localhost"
    )


def collector_client(*, base_url: str, **kwargs):
    runtime = load_runtime()
    if runtime.mode == "tenant":
        if base_url != collector_url():
            raise ValueError("Collector destination must match this tenant runtime")
        base_url = collector_url()
        if runtime.collector_port is None:
            kwargs["transport"] = httpx.HTTPTransport(uds=str(runtime.collector_socket))
    kwargs["trust_env"] = False
    return httpx.Client(base_url=base_url, **kwargs)


def namespaced_lock(legacy: int | str) -> int | str:
    """Separate tenant advisory locks without changing the legacy lock protocol."""
    import hashlib

    runtime = load_runtime()
    if runtime.mode == "legacy":
        return legacy
    if isinstance(legacy, str):
        return runtime.key + ":" + legacy
    return int.from_bytes(
        hashlib.sha256(f"{runtime.key}:{legacy}".encode()).digest()[:8], "big", signed=True
    )


def validate_runtime_path(path: Path) -> Path:
    """Explicit CLI/test overrides cannot read or publish another tenant's files."""
    runtime = load_runtime()
    path = Path(path)
    if runtime.mode == "tenant" and not path.resolve().is_relative_to(
        runtime.runtime_directory.resolve()
    ):
        raise ValueError("Path must remain inside this tenant runtime directory")
    return path
