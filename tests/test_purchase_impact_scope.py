"""Disabled venues cannot suppress an implicit forecast or widen user grants."""

from uuid import UUID

from app.web.purchase_impact_scope import forecast_departments
from app.web.sync_policy import SyncPolicy

A, B, HIDDEN = (UUID(int=i) for i in range(1, 4))


def test_default_uses_only_enabled_authorized_candidates(monkeypatch):
    monkeypatch.setattr(
        "app.web.purchase_impact_scope.read_policy",
        lambda db: SyncPolicy(departments=frozenset((str(A), str(HIDDEN)))),
    )
    assert forecast_departments(None, [A, B]) == [A]
    assert forecast_departments(None, [str(A), str(B)]) == [str(A)]
    assert forecast_departments(None, [B]) == []


def test_all_enabled_keeps_candidates(monkeypatch):
    monkeypatch.setattr("app.web.purchase_impact_scope.read_policy", lambda db: SyncPolicy())
    assert forecast_departments(None, [A, B]) == [A, B]


def test_explicit_frozen_selection_is_not_silently_dropped(monkeypatch):
    def no_policy(db):
        raise AssertionError("Explicit selection must reach the coverage check unchanged")

    monkeypatch.setattr("app.web.purchase_impact_scope.read_policy", no_policy)
    assert forecast_departments(None, [A, B], explicit=True) == [A, B]
