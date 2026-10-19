"""Worker restart policy uses fakes: no real documents, Telegram or database."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from psycopg_pool import PoolTimeout, TooManyRequests

from app import support_background
from app.documents import worker
from app.documents.config import DocumentSettings


def run_worker(monkeypatch, outcomes):
    state = {"cycles": 0, "waits": []}
    service = SimpleNamespace(database=object(), close=Mock())

    class Stop:
        def is_set(self):
            return state["cycles"] >= len(outcomes)

        def wait(self, seconds):
            state["waits"].append(seconds)

        def set(self):
            state["cycles"] = len(outcomes)

    @contextmanager
    def leader(_database):
        outcome = outcomes[state["cycles"]]
        state["cycles"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        yield None if outcome == "busy" else object()

    monkeypatch.setattr("sys.argv", ["worker"])
    monkeypatch.setattr(worker, "configure_http_logging", lambda: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setattr(worker, "DocumentSettings", lambda: DocumentSettings(
        _env_file=None, native_enabled=True, worker_enabled=True, bot_token="",
    ))
    monkeypatch.setattr(worker, "DocumentService", lambda _: service)
    monkeypatch.setattr(worker, "commercial_service", lambda *_: None)
    monkeypatch.setattr(worker, "Event", Stop)
    monkeypatch.setattr(worker, "worker_leader", leader)
    monkeypatch.setattr(worker, "catalog_due", lambda *_: False)
    jobs = Mock(return_value=True)
    monkeypatch.setattr(worker, "process_jobs", jobs)
    monkeypatch.setattr(support_background, "start_primary", lambda: None)
    monkeypatch.setattr(support_background, "close_primary", lambda _: None)
    failure = Mock()
    monkeypatch.setattr(support_background, "capture_failure", failure)
    return state, service, jobs, failure


@pytest.mark.parametrize("error", [PoolTimeout, TooManyRequests])
def test_sustained_pool_failure_exits_and_closes_for_supervisor_restart(monkeypatch, error):
    state, service, jobs, failure = run_worker(
        monkeypatch, [error("private connection details")] * 3
    )
    with pytest.raises(SystemExit, match="^document_worker_database_unavailable$"):
        worker.main()
    assert state["cycles"] == 3
    assert failure.call_count == 3
    jobs.assert_not_called()
    service.close.assert_called_once_with()


@pytest.mark.parametrize("recovery", ["healthy", "busy"])
def test_transient_pool_failure_resets_after_database_recovers(monkeypatch, recovery):
    state, service, jobs, _ = run_worker(monkeypatch, [
        PoolTimeout(), TooManyRequests(), recovery,
        TooManyRequests(), PoolTimeout(), recovery,
    ])
    worker.main()
    assert state["cycles"] == 6
    assert jobs.call_count == (2 if recovery == "healthy" else 0)
    service.close.assert_called_once_with()


def test_provider_errors_do_not_trigger_pool_restart(monkeypatch):
    _, service, _, _ = run_worker(monkeypatch, [ValueError("provider unavailable")] * 4)
    worker.main()
    service.close.assert_called_once_with()
