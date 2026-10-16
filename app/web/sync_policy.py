"""Apply saved synchronization choices to on-demand iiko reads, before cache lookup."""

from dataclasses import dataclass

from fastapi import HTTPException

from app.operation_settings import load_settings


@dataclass(frozen=True)
class SyncPolicy:
    revision: int = 0
    departments: frozenset[str] | None = None

    @property
    def key(self):
        return self.revision, self.departments

    def permits(self, ids):
        return self.departments is None or {str(value) for value in ids}.issubset(self.departments)

    def require(self, ids):
        if not self.permits(ids) or self.departments == frozenset():
            raise HTTPException(
                423,
                "Обновление одного из выбранных заведений отключено. "
                "Выберите заведения с включённой синхронизацией. "
                "Сохранённая история остаётся доступна.",
            )


def read_policy(db):
    saved = load_settings(db)
    selected = saved["sync"]
    return SyncPolicy(
        saved["revision"],
        None if selected["mode"] == "all" else frozenset(selected["department_ids"]),
    )


def guard_requests(db, bodies):
    """Called while holding the synchronization lock, also for queued jobs."""
    policy = read_policy(db)
    if policy.departments is None:
        return
    for body in bodies:
        selected = body.get("filters", {}).get("Department.Id", {})
        if selected.get("filterType") != "IncludeValues" or not selected.get("values"):
            policy.require(["unscoped"])
        policy.require(selected["values"])
