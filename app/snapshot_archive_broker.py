"""Linux-only archive boundary: peer Unix identity selects the immutable S3 prefix.

No TCP listener, bearer credentials, client-provided scope, or deletion operation.
Grant files are provisioned by the root operator and reloaded for each request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import socket
import socketserver
import stat
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from uuid import UUID

from app.saas_admin.runtime_process_identity import account_name, bind_socket
from app.tenancy.config import schema_names

REQUEST_DEADLINE_SECONDS = 60

ROUTE = re.compile(r"/v1/snapshots/([0-9a-f-]{36})/([0-9a-f]{64})")


def trusted_directory(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("Archive grant path must be absolute")
    for part in (path, *path.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Archive paths require root-owned non-writable ancestors")


def register_grant(directory: str, company_id: str, uid: int) -> None:
    """Root operator only; never consumes a tenant-controlled UID or schema."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Archive grants require the privileged Linux operator")
    company = str(UUID(company_id))
    entry = pwd.getpwuid(uid)
    if uid <= 0 or entry.pw_name != account_name(company):
        raise ValueError("Archive grant does not match the dedicated tenant identity")
    path = Path(directory)
    trusted_directory(path)
    value = {"company_id": company, "uid": uid}
    descriptor, temporary = tempfile.mkstemp(prefix=".grant-", dir=path)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path / f"{uid}.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


class GrantRegistry:
    def __init__(self, directory: str):
        self.directory = Path(directory)
        trusted_directory(self.directory)

    def scope(self, uid: int) -> str:
        if uid <= 0:
            raise PermissionError("Archive peer is not a tenant")
        trusted_directory(self.directory)
        try:
            descriptor = os.open(self.directory / f"{uid}.json", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0
                    or info.st_mode & 0o022
                    or info.st_size > 4096
                ):
                    raise ValueError("Untrusted archive grant")
                grant = json.load(stream)
            company = str(UUID(grant["company_id"]))
            if (
                set(grant) != {"company_id", "uid"}
                or grant["uid"] != uid
                or pwd.getpwuid(uid).pw_name != account_name(company)
                or sum(entry.pw_uid == uid for entry in pwd.getpwall()) != 1
            ):
                raise ValueError("Archive peer identity changed")
            return schema_names(company)["analytics"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PermissionError("Archive peer is not granted") from exc


def peer_uid(connection: socket.socket) -> int:
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise PermissionError("Linux peer credentials are required")
    _, uid, _ = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    return uid


class ArchiveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(REQUEST_DEADLINE_SECONDS)
        super().setup()
        # Socket timeouts alone are inactivity limits: a drip-fed request can
        # otherwise monopolize all worker slots indefinitely, even in headers.
        self.deadline = threading.Timer(REQUEST_DEADLINE_SECONDS, self.expire_request)
        self.deadline.daemon = True
        self.deadline.start()

    def expire_request(self):
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.deadline.cancel()
        super().finish()

    def log_message(self, *_):
        pass  # Payloads, object paths and provider errors are never access-log material.

    def reply(self, status: int, body: bytes = b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def dispatch(self, *, upload: bool):
        from app.snapshot_archive import MAX_OBJECT_BYTES, object_key

        try:
            scope = self.server.registry.scope(peer_uid(self.connection))
        except PermissionError:
            self.reply(403)
            return
        route = ROUTE.fullmatch(self.path)
        if route is None:
            self.reply(400)
            return
        snapshot_id, digest = route.groups()
        try:
            if str(UUID(snapshot_id)) != snapshot_id:
                raise ValueError("Noncanonical UUID")
            key = object_key(scope, snapshot_id, digest)
        except ValueError:
            self.reply(400)
            return
        try:
            if upload:
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                    self.reply(400)
                    return
                try:
                    length = int(lengths[0])
                except ValueError:
                    self.reply(400)
                    return
                if length <= 0 or length > MAX_OBJECT_BYTES:
                    self.reply(413)
                    return
                data = self.rfile.read(length)
                if len(data) != length or hashlib.sha256(data).hexdigest() != digest:
                    self.reply(400)
                    return
                self.server.storage.put(key, data)
            verified = self.server.storage.get(key)
            if len(verified) > MAX_OBJECT_BYTES or hashlib.sha256(verified).hexdigest() != digest:
                self.reply(502)
                return
            if upload:
                self.reply(
                    200,
                    json.dumps(
                        {
                            "object_key": key,
                            "object_sha256": digest,
                            "object_bytes": len(verified),
                            "format_version": 1,
                        }
                    ).encode(),
                )
            else:
                self.reply(200, verified, "application/octet-stream")
        except Exception:
            # Do not expose S3 exception bodies (they can contain signed requests).
            self.reply(503)

    def do_PUT(self):
        self.dispatch(upload=True)

    def do_GET(self):
        self.dispatch(upload=False)


class ArchiveServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, bound: socket.socket, registry, storage, concurrency=2):
        super().__init__(bound.getsockname(), ArchiveHandler, bind_and_activate=False)
        self.socket.close()
        self.socket = bound
        self.registry = registry
        self.storage = storage
        self.slots = threading.BoundedSemaphore(concurrency)
        self.server_activate()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass


def main():
    from app.snapshot_archive import ArchiveConfig, S3Storage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--grants", required=True)
    arguments = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Archive broker requires Linux SO_PEERCRED")
    path = Path(arguments.socket)
    trusted_directory(path.parent)
    registry = GrantRegistry(arguments.grants)
    config = ArchiveConfig.from_env()
    if config.mode != "s3":
        parser.error("Broker requires direct S3 configuration")
    with bind_socket(path, 0o666) as bound:
        with ArchiveServer(bound, registry, S3Storage(config)) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
