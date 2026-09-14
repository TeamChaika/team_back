"""Verify container startup with an isolated database, never production credentials."""

import subprocess
import sys
import time
from uuid import uuid4


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


PROBE = """
import json, sys, time, urllib.request, urllib.error
base = sys.argv[1]
for attempt in range(40):
    try:
        with urllib.request.urlopen(base + '/api/health', timeout=2) as r:
            assert r.status == 200 and json.load(r) == {'status': 'ok'}
        break
    except (OSError, AssertionError):
        if attempt == 39:
            raise
        time.sleep(0.5)
with urllib.request.urlopen(base + '/', timeout=2) as r:
    assert r.status == 200 and json.load(r)['service'] == 'Chaika Team API'
try:
    urllib.request.urlopen(base + '/api/me', timeout=2)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError('Anonymous access was unexpectedly allowed')
print('HTTP health/root/auth checks passed')
"""


def main() -> None:
    image = sys.argv[1] if len(sys.argv) > 1 else "chaika-backend:ci"
    prefix = "chaika-smoke-" + uuid4().hex[:12]
    db_name = prefix + "-db"
    containers = []
    docker("network", "create", "--internal", prefix)
    try:
        containers.append(db_name)
        docker(
            "run",
            "-d",
            "--name",
            db_name,
            "--network",
            prefix,
            "--network-alias",
            "db",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "postgres:17-alpine",
        )
        for attempt in range(40):
            ready = subprocess.run(
                ["docker", "exec", db_name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ready.returncode == 0:
                break
            if attempt == 39:
                raise RuntimeError("Isolated test database did not start")
            time.sleep(0.5)

        # Probe from another container, so binding only to localhost cannot pass.
        for port in (8000, 9123):
            app_name = prefix + "-app-" + str(port)
            containers.append(app_name)
            env = [] if port == 8000 else ["-e", f"PORT={port}"]
            docker(
                "run",
                "-d",
                "--name",
                app_name,
                "--network",
                prefix,
                "-e",
                "CHAIKA_DATABASE_URL=postgresql://postgres:postgres@db:5432/postgres",
                *env,
                image,
            )
            print(
                docker(
                    "run",
                    "--rm",
                    "--network",
                    prefix,
                    image,
                    "python",
                    "-c",
                    PROBE,
                    f"http://{app_name}:{port}",
                )
            )
            assert docker("inspect", "--format", "{{.State.Running}}", app_name) == "true"
            assert docker("inspect", "--format", "{{.RestartCount}}", app_name) == "0"
            assert docker("exec", app_name, "id", "-u") != "0"
            assert docker("exec", app_name, "test", "!", "-e", "/app/.env") == ""
            print(f"PASS: port {port}, non-root process, no restarts")
            docker("stop", "--time", "5", app_name)
    except Exception:
        for container in containers:
            # These containers contain synthetic credentials and no business data.
            subprocess.run(["docker", "logs", "--tail", "30", container], check=False)
        raise
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", prefix], capture_output=True, check=False)


if __name__ == "__main__":
    main()
