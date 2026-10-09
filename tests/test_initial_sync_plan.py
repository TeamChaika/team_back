from datetime import date

import pytest

from app.saas_admin.initial_sync_plan import (
    REQUIRED_JOBS,
    initial_sync_compatible,
    initial_sync_plan,
)
from app.saas_admin.runtime_registry import REQUIRED_CHECKS, SETUP_CHECKS, WORKING_CHECKS, completed


def current_proof():
    return {
        "ok": True,
        "evidence": {
            **initial_sync_plan(date(2026, 10, 8), date(2026, 10, 8)),
            "completed_jobs": list(REQUIRED_JOBS),
        },
    }


@pytest.mark.parametrize("change", ["legacy", "version", "documents", "windows", "jobs"])
def test_old_or_partial_history_proof_cannot_declare_readiness(change):
    check = current_proof()
    assert initial_sync_compatible(check)
    if change == "legacy":
        del check["evidence"]["plan_version"]
    elif change == "version":
        check["evidence"]["plan_version"] = 1
    elif change == "documents":
        check["evidence"]["document_resources"].remove("outgoing")
    elif change == "windows":
        check["evidence"]["cash_shift_windows"] = []
    else:
        check["evidence"]["completed_jobs"].remove("sync_cash_shifts")
    assert not initial_sync_compatible(check)
    checks = {step: {"ok": True, "evidence": "test-only"} for step in REQUIRED_CHECKS}
    checks["initial_sync"] = check
    for required in (REQUIRED_CHECKS, WORKING_CHECKS, SETUP_CHECKS):
        assert not completed(checks, required)
    checks["initial_sync"] = current_proof()
    assert completed(checks, REQUIRED_CHECKS)
