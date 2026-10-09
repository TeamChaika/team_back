"""Identity boundaries and hostile tenant files must fail without stopping peers."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.saas_admin import runtime_process_identity as isolation
from app.saas_admin.runtime_fleet import FleetSupervisor


def test_account_names_are_complete_uuid_derived_and_fit_linux():
    first, second = uuid4(), uuid4()
    assert len(isolation.account_name(first)) == 29
    assert isolation.account_name(first) != isolation.account_name(second)
    assert isolation.account_name(str(first).upper()) == isolation.account_name(first)
    with pytest.raises(ValueError):
        isolation.account_name("../../root")


def test_missing_policy_does_not_fall_back_to_shared_user():
    for value in ({}, {"process_isolation": {"mode": "auto"}}):
        with pytest.raises(ValueError, match="Explicit"):
            isolation.policy(value)
    assert isolation.policy({"process_isolation": {"mode": "local-test"}})["mode"] == "local-test"


def test_production_factory_refuses_local_policy_before_opening_database(tmp_path):
    from app.saas_admin.deployment import create_deployment_app

    path = tmp_path / "fleet.json"
    path.write_text(
        json.dumps({"operator_template": {"process_isolation": {"mode": "local-test"}}})
    )
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Production requires Linux"):
        create_deployment_app(tmp_path, mode="production", runtime_config=path)


@pytest.mark.parametrize("uid,gid,mode", [(1, 987, 0o640), (0, 986, 0o640), (0, 987, 0o660)])
def test_linux_operator_file_rejects_wrong_owner_group_or_write_access(
    tmp_path, monkeypatch, uid, gid, mode
):
    path = tmp_path / "operator.json"
    config = {"process_isolation": {"mode": "linux", "central_group": "central"}}
    path.write_text(json.dumps(config))
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=987))
    monkeypatch.setattr(
        isolation.os,
        "fstat",
        lambda fd: SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=gid),
    )
    with pytest.raises(ValueError, match="root:central"):
        isolation.read_operator_json(path)


@pytest.fixture
def local_identity(tmp_path):
    return isolation.ProcessIdentity(
        {"process_isolation": {"mode": "local-test"}, "runtime_root": str(tmp_path)},
        uuid4(),
        create=True,
    )


@pytest.mark.parametrize("kind", ["fifo", "symlink", "public", "oversize"])
def test_root_child_read_rejects_hostile_files_without_blocking(local_identity, kind):
    path = local_identity.directory / "environment.json"
    if kind == "fifo":
        os.mkfifo(path, mode=0o600)
    elif kind == "symlink":
        target = local_identity.directory / "target"
        target.write_text("secret")
        path.symlink_to(target)
    else:
        path.write_text("x" * (33 if kind == "oversize" else 1))
        path.chmod(0o644 if kind == "public" else 0o600)
    with pytest.raises((ValueError, OSError)):
        local_identity.read_private(path.name, limit=32)


def test_child_read_and_copies_stay_private(local_identity):
    local_identity.write_private("verifier.key", "a" * 48)
    assert local_identity.read_private("verifier.key") == "a" * 48
    assert stat.S_IMODE((local_identity.directory / "verifier.key").stat().st_mode) == 0o600


def test_exec_drops_groups_gid_uid_and_descriptors_before_executing(monkeypatch):
    identity = object.__new__(isolation.ProcessIdentity)
    identity.uid, identity.gid = 1011, 1012
    identity.spawn_options = lambda: {"cwd": "/tenant", "umask": 0o077}
    calls = []
    for name in ("chdir", "umask", "setgroups", "setresgid", "setresuid", "closerange", "execve"):
        monkeypatch.setattr(
            isolation.os, name, lambda *args, name=name: calls.append((name, args)), raising=False
        )
    monkeypatch.setattr(isolation.os, "sysconf", lambda name: 100)
    identity.exec(["/python", "-m", "app"], {"OWN": "value"})
    assert [call[0] for call in calls] == [
        "chdir",
        "umask",
        "setgroups",
        "setresgid",
        "setresuid",
        "closerange",
        "execve",
    ]
    assert calls[2][1] == ([],)
    assert calls[3][1] == (1012, 1012, 1012)
    assert calls[4][1] == (1011, 1011, 1011)


def test_failed_tenant_launch_does_not_stop_neighbor_and_backs_off(tmp_path, monkeypatch, caplog):
    other = SimpleNamespace(poll=lambda: None)
    supervisor = FleetSupervisor(SimpleNamespace())
    supervisor.children[("neighbor", "run-portal")] = other
    calls = []

    def fail(*args):
        calls.append(args)
        raise ValueError("SECRET FILE CONTENT SHOULD NOT BE LOGGED")

    monkeypatch.setattr(supervisor, "_start", fail)
    path = tmp_path / "bad.json"
    assert supervisor.start(path, "run-portal", 1) is False
    assert supervisor.start(path, "run-portal", 1) is False
    assert len(calls) == 1
    assert supervisor.children[("neighbor", "run-portal")] is other
    assert "SECRET FILE" not in caplog.text
    assert supervisor.launch_errors[(str(path), "run-portal")] == "runtime_launch_failed"


def test_corrupt_binding_does_not_prevent_next_company_tick(tmp_path, monkeypatch):
    for name in ("c_bad.json", "c_good.json"):
        (tmp_path / name).touch()
    supervisor = FleetSupervisor(SimpleNamespace(root=tmp_path, prepare_pending=lambda: None))
    seen = []

    def tick(path):
        seen.append(path.name)
        if path.name == "c_bad.json":
            raise ValueError("bad identity")

    monkeypatch.setattr(supervisor, "tick_company", tick)
    supervisor.tick()
    assert set(seen) == {"c_bad.json", "c_good.json"}


def test_bound_socket_is_private_and_live_socket_is_never_replaced(tmp_path):
    # Keep the Unix path short on macOS too.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="rcsock-") as directory:
        path = Path(directory) / "portal.sock"
        with isolation.bind_socket(path, 0o660) as bound:
            bound.listen()
            inode = path.stat().st_ino
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
            with pytest.raises(ValueError, match="live process"):
                isolation.bind_socket(path, 0o660)
            assert path.stat().st_ino == inode


@pytest.mark.skipif(
    isolation.sys.platform != "linux"
    or os.geteuid() != 0
    or os.environ.get("RESTCONTROL_LINUX_IDENTITY_TESTS") != "1"
    or not Path("/.dockerenv").exists(),
    reason="requires explicit disposable root Linux container; never run on the production host",
)
def test_real_linux_users_cannot_read_central_or_neighbor():
    import subprocess
    import tempfile

    isolated_base = tempfile.TemporaryDirectory(prefix="rc-identity-", dir="/var/lib")
    tmp_path = Path(isolated_base.name)
    tmp_path.chmod(0o755)

    # All paths, code and interpreter must be root-owned in the disposable image.
    config = {
        "process_isolation": {"mode": "linux", "central_group": "nogroup"},
        "runtime_root": str(tmp_path / "tenants"),
    }
    identities = []
    try:
        for _ in range(2):
            identities.append(isolation.ProcessIdentity(config, uuid4(), create=True))
        for identity in identities:
            identity.write_private("own.key", "own-secret")
        central = tmp_path / "central.key"
        central.write_text("central-secret")
        central.chmod(0o600)
        probe = (
            "from pathlib import Path; import sys;\nfor p in sys.argv[1:]:\n"
            " try: print(Path(p).read_text())\n except PermissionError: print('denied')\n"
        )
        for own, other in (identities, list(reversed(identities))):
            result = subprocess.run(
                [
                    isolation.sys.executable,
                    "-c",
                    probe,
                    str(own.directory / "own.key"),
                    str(other.directory / "own.key"),
                    str(central),
                ],
                capture_output=True,
                text=True,
                check=True,
                **own.spawn_options(),
            )
            assert result.stdout.splitlines() == ["own-secret", "denied", "denied"]
        # The gateway's central GID may connect; another tenant UID/GID may not.
        own, other = identities
        socket_path = own.directory / "acceptance.sock"
        server_code = (
            "import os,socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); "
            "os.chmod(sys.argv[1],0o660); s.listen(); print('ready',flush=True); "
            "c,_=s.accept(); c.sendall(b'ok'); c.close()"
        )
        client_code = (
            "import socket,sys; s=socket.socket(socket.AF_UNIX);\n"
            "try: s.connect(sys.argv[1]); print(s.recv(2).decode())\n"
            "except PermissionError: print('denied')\n"
        )
        with subprocess.Popen(
            [isolation.sys.executable, "-c", server_code, str(socket_path)],
            stdout=subprocess.PIPE,
            text=True,
            **own.spawn_options(),
        ) as server:
            try:
                import select

                assert select.select([server.stdout], [], [], 5)[0]
                assert server.stdout.readline().strip() == "ready"
                assert socket_path.stat().st_gid == own.policy["central_gid"]
                denied = subprocess.run(
                    [isolation.sys.executable, "-c", client_code, str(socket_path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                    **other.spawn_options(),
                )
                assert denied.stdout.strip() == "denied"
                central_identity = isolation.pwd.getpwnam("nobody")
                allowed = subprocess.run(
                    [isolation.sys.executable, "-c", client_code, str(socket_path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                    user=central_identity.pw_uid,
                    group=own.policy["central_gid"],
                    extra_groups=[],
                    cwd=str(tmp_path),
                )
                assert allowed.stdout.strip() == "ok"
                server.wait(timeout=5)
            finally:
                if server.poll() is None:
                    server.kill()
                    server.wait(timeout=5)
    finally:
        for identity in identities:
            subprocess.run(
                ["/usr/sbin/userdel", isolation.account_name(identity.company_id)], check=True
            )
        isolated_base.cleanup()
