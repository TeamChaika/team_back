"""Fail closed when a report cannot establish the warehouses of its rows."""

from fastapi import HTTPException


def warehouse_ids(scope):
    """None means unrestricted; an empty restricted scope never means all stores."""
    if not getattr(scope, "warehouse_restricted", False):
        return None
    stores = tuple(sorted({str(store) for store in scope.store_ids}))
    if not stores:
        raise HTTPException(403, "Нет доступных складов.")
    return stores


def require_department_report(scope):
    if getattr(scope, "warehouse_restricted", False):
        raise HTTPException(
            403,
            "Этот отчёт пока не поддерживает доступ по отдельным складам. "
            "Общий итог заведения недоступен.",
        )
