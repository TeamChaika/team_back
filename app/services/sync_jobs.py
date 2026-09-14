"""Durable local refresh requests and detached workers for the current backend host."""

import fcntl
import json
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg

from app.core.config import BACKEND_DIR, Settings
from app.progress import describe, read_json
from app.schemas.sync_jobs import RefreshStatus, ResourceProgress
from app.sync_references import SyncError, reference_lock

ROOT = BACKEND_DIR / ".local/sync"


class SyncJobError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 503):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(code)


def now() -> datetime:
    return datetime.now(UTC)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def job_alive(pid: int | None, job_id: UUID) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat=,command="],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    fields = result.stdout.strip().split(None, 1)
    if len(fields) != 2 or fields[0].startswith("Z"):
        return False
    args = shlex.split(fields[1])
    return any(
        args[i : i + 4] == ["-m", "app.sync_refresh", "--job-id", str(job_id)]
        for i in range(len(args))
    )


class SyncJobsService:
    def __init__(self, settings: Settings, directory: Path = ROOT):
        self.settings, self.directory = settings, directory

    def job_directory(self, job_id: UUID) -> Path:
        return self.directory / "jobs" / str(job_id)

    @contextmanager
    def launch_lock(self, *, wait: bool = False):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.directory / "refresh-launch.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                raise SyncJobError(
                    "sync_start_busy", "Другой запрос запуска уже обрабатывается.", 409
                ) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def preflight(self):
        if not self.settings.database_url.get_secret_value():
            raise SyncJobError("database_not_configured", "База данных не настроена.")
        try:
            with (
                psycopg.connect(
                    self.settings.database_url.get_secret_value(),
                    autocommit=True,
                    connect_timeout=5,
                    options="-c statement_timeout=5000",
                ) as db,
                reference_lock(db),
            ):
                pass
        except SyncError:
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        except psycopg.Error:
            raise SyncJobError("sync_database_unavailable", "База данных недоступна.") from None

    def spawn(self, job_id: UUID) -> int:
        path = self.job_directory(job_id) / "worker.log"
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "ab") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "app.sync_refresh",
                    "--job-id",
                    str(job_id),
                    "--directory",
                    str(self.directory.resolve()),
                ],
                cwd=BACKEND_DIR,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        Thread(target=process.wait, daemon=True).start()
        return process.pid

    def start(self, job_id: UUID) -> tuple[RefreshStatus, bool]:
        with self.launch_lock():
            path = self.job_directory(job_id) / "job.json"
            if path.exists():
                return self.get(job_id), False
            active = read_json(self.directory / "refresh-active.json")
            if active:
                previous = self.get(UUID(active["job_id"]))
                if previous.status in {"accepted", "running"}:
                    raise SyncJobError(
                        "sync_already_running", "Синхронизация уже выполняется.", 409
                    )
            self.preflight()
            timestamp = now()
            end = timestamp.astimezone(ZoneInfo("Europe/Simferopol")).date()
            manifest = {
                "job_id": str(job_id),
                "status": "accepted",
                "date_from": (end - timedelta(days=59)).isoformat(),
                "date_to": end.isoformat(),
                "created_at": timestamp.isoformat(),
                "finished_at": None,
                "error_code": None,
                "pid": None,
            }
            write_json(path, manifest)
            write_json(self.directory / "refresh-active.json", {"job_id": str(job_id)})
            try:
                manifest["pid"] = self.spawn(job_id)
            except OSError:
                manifest.update(
                    status="failed",
                    error_code="sync_worker_start_failed",
                    finished_at=now().isoformat(),
                )
                write_json(path, manifest)
                raise SyncJobError(
                    "sync_worker_start_failed", "Не удалось запустить загрузчик."
                ) from None
            write_json(path, manifest)
            return self.get(job_id), True

    def get(self, job_id: UUID) -> RefreshStatus:
        directory = self.job_directory(job_id)
        manifest = read_json(directory / "job.json")
        if manifest is None:
            raise SyncJobError("sync_job_not_found", "Задание не найдено.", 404)
        alive = job_alive(manifest.get("pid"), job_id)
        status = manifest["status"]
        if status in {"accepted", "running"} and not alive:
            launching = (
                status == "accepted"
                and (now() - datetime.fromisoformat(manifest["created_at"])).total_seconds() < 30
            )
            if not launching:
                status = "interrupted"
        resources = []
        for resource in ("invoices", "writeoffs"):
            report = read_json(directory / f"{resource}.json")
            projection = describe(resource, report, manifest, alive)
            if report is None:
                projection["status"] = "starting" if status in {"accepted", "running"} else status
            resources.append(ResourceProgress.model_validate(projection))
        return RefreshStatus(
            job_id=job_id,
            status=status,
            date_from=manifest["date_from"],
            date_to=manifest["date_to"],
            created_at=manifest["created_at"],
            finished_at=manifest["finished_at"],
            error_code="sync_worker_interrupted"
            if status == "interrupted"
            else manifest["error_code"],
            resources=resources,
        )
