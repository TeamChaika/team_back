"""Closed-CLI Linux identity boundary for the privileged tenant fleet.

The HTTP application only reads root-owned manifests. Account creation, ownership
changes and privilege dropping are available to the supervised operator CLI only.
Local tests must opt in explicitly; an absent policy never means shared-UID launch.
"""

from __future__ import annotations

import base64
import ctypes
import grp
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypedDict
from uuid import UUID

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from app.tenancy.config import TenantRuntime


class LocalPolicy(TypedDict):
    mode: Literal["local-test"]


class LinuxPolicy(TypedDict):
    mode: Literal["linux"]
    central_group: str
    central_gid: int


class SpawnOptions(TypedDict, total=False):
    user: int
    group: int
    extra_groups: list[int]
    cwd: str
    umask: int


Configuration = Mapping[str, Any]


def account_name(company_id: str | UUID) -> str:
    encoded = base64.b32encode(UUID(str(company_id)).bytes).decode().rstrip("=").lower()
    return "rc_" + encoded


def policy(configuration: Configuration) -> LocalPolicy | LinuxPolicy:
    value = configuration.get("process_isolation")
    if not isinstance(value, dict) or value.get("mode") not in {"linux", "local-test"}:
        raise ValueError("Explicit process_isolation mode is required")
    if value["mode"] == "local-test":
        if set(value) != {"mode"}:
            raise ValueError("Invalid local-test process isolation")
        return {"mode": "local-test"}
    if set(value) != {"mode", "central_group"} or sys.platform != "linux":
        raise ValueError("Linux process isolation requires an explicit central group")
    group = grp.getgrnam(value["central_group"])
    if group.gr_gid == 0:
        raise ValueError("The central group must not be root")
    return {"mode": "linux", "central_group": value["central_group"], "central_gid": group.gr_gid}


def read_operator_json(filename: str | Path) -> dict[str, Any]:
    """Read legacy 0600 or root-owned 0640 with its explicit trusted central GID."""
    path = Path(filename)
    if path.is_symlink():
        raise ValueError("Operator configuration must not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Operator configuration must be a regular file")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Operator configuration must be a JSON object")
    template = value.get("operator_template", value)
    if not isinstance(template, dict):
        raise ValueError("Operator template must be a JSON object")
    selected = policy(template)
    if selected["mode"] == "local-test":
        if info.st_mode & 0o077:
            raise ValueError("Local operator configuration must be private")
    elif (
        info.st_uid != 0
        or info.st_gid != selected["central_gid"]
        or stat.S_IMODE(info.st_mode) != 0o640
    ):
        raise ValueError("Linux operator configuration must be root:central 0640")
    return value


def _root() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Linux fleet mutation requires the privileged operator CLI")


def _absolute(path: str | Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise ValueError("Runtime paths must be absolute and contain no symlink components")
    return path


def _trusted_ancestors(path: Path) -> None:
    for parent in (path, *path.parents):
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Privileged runtime paths require root-owned non-writable ancestors")


def central_directory(
    path: str | Path, configuration: Configuration, *, create: bool = False
) -> Path:
    selected = policy(configuration)
    path = _absolute(path)
    if selected["mode"] == "local-test":
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Central directory must be private")
        return path
    if create and not path.exists():
        _root()
        _trusted_ancestors(path.parent)
        if not path.exists():
            path.mkdir(mode=0o750)
            os.chown(path, 0, selected["central_gid"])
            os.chmod(path, 0o750)
    info = path.stat()
    _trusted_ancestors(path)
    if info.st_gid != selected["central_gid"] or stat.S_IMODE(info.st_mode) != 0o750:
        raise ValueError("Central directory must be root:central 0750")
    return path


def write_central(path: str | Path, data: str, configuration: Configuration) -> None:
    selected = policy(configuration)
    if selected["mode"] == "linux":
        _root()
    central_directory(Path(path).parent, configuration)
    _atomic(
        path,
        data,
        0 if selected["mode"] == "linux" else None,
        selected.get("central_gid"),
        0o640 if selected["mode"] == "linux" else 0o600,
    )


def read_central_secret(filename: str | Path, configuration: Configuration) -> str:
    selected = policy(configuration)
    path = Path(filename)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Central capability must be a regular file")
        if selected["mode"] == "linux":
            if (
                info.st_uid != 0
                or info.st_gid != selected["central_gid"]
                or stat.S_IMODE(info.st_mode) != 0o640
            ):
                raise ValueError("Central capability must be root:central 0640")
        elif info.st_mode & 0o077:
            raise ValueError("Local capability must be private")
        secret = stream.read().strip()
    if len(secret) < 32 or not secret.isascii() or any(c.isspace() for c in secret):
        raise ValueError("Invalid central capability")
    return secret


def validate_child_directory(runtime: TenantRuntime, environment: Mapping[str, str]) -> None:
    """Only the explicit Linux launcher may opt into central-group traversal."""
    path = runtime.runtime_directory
    if path.is_symlink():
        raise ValueError("Runtime directory must not be a symlink")
    info = path.stat()
    uid = environment.get("RESTCONTROL_TENANT_PROCESS_UID")
    central_gid = environment.get("RESTCONTROL_TENANT_CENTRAL_GID")
    if uid is None and central_gid is None:
        if info.st_mode & 0o077:
            raise ValueError("Local tenant runtime directory must be private")
        return
    if (
        uid is None
        or central_gid is None
        or sys.platform != "linux"
        or int(uid) != os.getuid()
        or int(uid) == 0
        or info.st_uid != int(uid)
        or info.st_gid != int(central_gid)
        or int(central_gid) in {0, os.getgid()}
        or os.getgroups()
        or stat.S_IMODE(info.st_mode) != 0o2750
    ):
        raise ValueError("Tenant process or central directory identity is invalid")


def bind_socket(filename: str | Path, mode: int) -> socket.socket:
    """Bind with explicit permissions; refuse to replace any live/foreign socket."""
    path = Path(filename)
    if len(os.fsencode(str(path))) >= 104:
        raise ValueError("Unix socket path is too long")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Refusing an unowned or unexpected socket")
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                if path.lstat().st_ino != info.st_ino:
                    raise ValueError("Socket changed during validation") from None
                path.unlink()
            else:
                raise ValueError("A live process already owns this socket")
    bound = socket.socket(socket.AF_UNIX)
    try:
        bound.bind(str(path))
        path.chmod(mode)
        bound.setblocking(False)
        return bound
    except BaseException:
        bound.close()
        raise


def serve_verifier_socket(app: ASGIApp, filename: str | Path, configuration: Configuration) -> None:
    import uvicorn

    selected = policy(configuration)
    path = _absolute(filename)
    info = path.parent.stat()
    if selected["mode"] == "linux":
        _trusted_ancestors(path.parent.parent)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o711:
            raise ValueError("Verifier socket needs its own trusted traversal-only 0711 directory")
    elif info.st_mode & 0o077:
        raise ValueError("Local verifier socket must be private")
    with bind_socket(path, 0o666 if selected["mode"] == "linux" else 0o600) as bound:
        uvicorn.Server(uvicorn.Config(app, access_log=False, proxy_headers=False)).run(
            sockets=[bound]
        )


def _atomic(path: str | Path, data: str, uid: int | None, gid: int | None, mode: int) -> None:
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".rc-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            if uid is not None:
                if gid is None:
                    raise ValueError("An owned private file requires an explicit group")
                os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ProcessIdentity:
    def __init__(
        self, configuration: Configuration, company_id: str | UUID, *, create: bool = False
    ) -> None:
        self.configuration = configuration
        self.policy = policy(configuration)
        self.company_id = UUID(str(company_id))
        self.directory = _absolute(
            Path(configuration["runtime_root"]) / ("c_" + self.company_id.hex)
        )
        self.uid: int | None = None
        self.gid: int | None = None
        if self.policy["mode"] == "local-test":
            if create:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            return
        root = self.directory.parent
        if create:
            _root()
            _trusted_ancestors(root.parent)
            if not root.exists():
                root.mkdir(mode=0o711)
                os.chmod(root, 0o711)
        _trusted_ancestors(root)
        if stat.S_IMODE(root.stat().st_mode) != 0o711:
            raise ValueError("Tenant root must be root-owned 0711")
        name = account_name(self.company_id)
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            if not create:
                raise ValueError("The dedicated tenant Unix account is missing") from None
            subprocess.run(
                [
                    "/usr/sbin/useradd",
                    "--system",
                    "--user-group",
                    "--no-create-home",
                    "--home-dir",
                    str(self.directory),
                    "--shell",
                    "/usr/sbin/nologin",
                    name,
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            entry = pwd.getpwnam(name)
        group = grp.getgrnam(name)
        central_gid = self.policy["central_gid"]
        if (
            entry.pw_uid == 0
            or entry.pw_gid != group.gr_gid
            or group.gr_gid in {0, central_gid}
            or entry.pw_dir != str(self.directory)
            or entry.pw_shell != "/usr/sbin/nologin"
            or group.gr_mem
            or any(p.pw_uid == entry.pw_uid and p.pw_name != name for p in pwd.getpwall())
            or any(p.pw_gid == group.gr_gid and p.pw_name != name for p in pwd.getpwall())
            or any(name in g.gr_mem for g in grp.getgrall())
        ):
            raise ValueError("Tenant account must have its own UID/GID and no supplementary groups")
        self.uid, self.gid = entry.pw_uid, entry.pw_gid
        if create and not self.directory.exists():
            self.directory.mkdir(mode=0o700)
            os.chown(self.directory, self.uid, central_gid)
            os.chmod(self.directory, 0o2750)
        self.validate_directory()

    def validate_directory(self) -> None:
        info = self.directory.stat()
        if self.policy["mode"] == "local-test":
            if info.st_mode & 0o077:
                raise ValueError("Local tenant directory must be private")
        elif (
            info.st_uid != self.uid
            or info.st_gid != self.policy["central_gid"]
            or stat.S_IMODE(info.st_mode) != 0o2750
        ):
            raise ValueError("Tenant directory must be own-UID:central 02750")

    def write_private(self, name: str, data: str) -> None:
        self.validate_directory()
        if self.policy["mode"] == "linux":
            _root()
        if Path(name).name != name:
            raise ValueError("Private child filename must be one path component")
        _atomic(self.directory / name, data, self.uid, self.gid, 0o600)

    def read_private(self, name: str, *, limit: int = 1024 * 1024) -> str:
        """Never block the root supervisor on a tenant FIFO or follow a symlink."""
        self.validate_directory()
        if Path(name).name != name:
            raise ValueError("Private child filename must be one path component")
        path = self.directory / name
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            expected_uid = self.uid if self.uid is not None else os.geteuid()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != expected_uid
                or info.st_mode & 0o077
                or info.st_size > limit
            ):
                raise ValueError("Invalid private child file")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Private child file exceeds its size limit")
        return data.decode("utf-8")

    def spawn_options(self) -> SpawnOptions:
        self.validate_directory()
        if self.policy["mode"] == "local-test":
            return {}
        _root()
        if self.uid is None or self.gid is None:
            raise ValueError("Linux process identity is incomplete")
        code_root = Path(__file__).resolve().parents[2]
        _trusted_ancestors(code_root)
        _trusted_ancestors(Path(sys.executable).resolve())
        # The root supervisor keeps its present privileges, while every execed
        # child is prevented from gaining privileges through setuid binaries.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "Cannot lock child privilege escalation")
        return {
            "user": self.uid,
            "group": self.gid,
            "extra_groups": [],
            "cwd": str(self.directory),
            "umask": 0o077,
        }

    def environment(self) -> dict[str, str]:
        if self.policy["mode"] == "local-test":
            return {}
        return {
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "RESTCONTROL_TENANT_CENTRAL_GID": str(self.policy["central_gid"]),
            "RESTCONTROL_TENANT_PROCESS_UID": str(self.uid),
        }

    def exec(self, command: Sequence[str], environment: Mapping[str, str]) -> NoReturn:
        options = self.spawn_options()
        if options:
            if self.uid is None or self.gid is None:
                raise ValueError("Linux exec identity is incomplete")
            os.chdir(options["cwd"])
            os.umask(options["umask"])
            os.setgroups([])
            os.setresgid(self.gid, self.gid, self.gid)
            os.setresuid(self.uid, self.uid, self.uid)
            os.closerange(3, os.sysconf("SC_OPEN_MAX"))
        os.execve(command[0], command, environment)
