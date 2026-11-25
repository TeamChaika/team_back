"""Menu permissions are enforced at the API boundary as well as in navigation."""

from fastapi import HTTPException

SECTIONS = {
    "overview": "Обзор",
    "indicators": "Показатели",
    "sales": "Продажи",
    "deposits": "Депозиты",
    "cash-shifts": "Кассовые смены",
    "invoices": "Приходные накладные",
    "purchase-prices": "Закупочные цены",
    "outgoing": "Реализация",
    "transfers": "Перемещения",
    "writeoffs": "Списания",
    "products": "Номенклатура",
    "charts": "Технологические карты",
    "balances": "Остатки на складах",
    "employees": "Сотрудники iiko",
    "events": "События заказов",
    "status": "Статус данных",
    "profit-loss": "Прибыли и убытки · ввод расходов",
    "profit-loss-accounting": "Прибыли и убытки · бухгалтерия",
}
FINANCE_SECTIONS = {"profit-loss", "profit-loss-accounting"}
IIKO_SECTIONS = set(SECTIONS) - {"deposits"}
DOCUMENT_SECTIONS = {"transfers", "writeoffs"}
ANALYTICS_SECTIONS = IIKO_SECTIONS - DOCUMENT_SECTIONS


def finance_level(user):
    if user.get("warehouse_scope_mode") == "selected":
        return None
    if user.get("role") == "owner":
        return "owner"
    grants = user.get("sections", ())
    if "profit-loss-accounting" in grants:
        return "accountant"
    if "profit-loss" in grants:
        return "manager"
    return None


def sections_for(user):
    # Older in-process integrations may still supply the legacy profile shape.
    if "sections" not in user:
        sections = (
            ["deposits"]
            if user["role"] == "deposits"
            else [key for key in SECTIONS if key not in FINANCE_SECTIONS]
        )
    else:
        sections = [key for key in user["sections"] if key in SECTIONS]
    if finance_level(user) and "profit-loss" not in sections:
        sections.append("profit-loss")
    return sections


def require_section(user, section):
    if section not in sections_for(user):
        raise HTTPException(403, "Нет доступа к этому разделу.")


def require_admin(user):
    if not user.get("is_portal_admin"):
        raise HTTPException(403, "Управление доступно только администратору.")


def section_for_path(path):
    parts = path.strip("/").split("/")
    resource = parts[1] if len(parts) > 1 else ""
    if resource == "resources":
        return parts[2] if len(parts) > 2 else ""
    return {
        "assistant": "purchase-prices",
        "discount-details": "sales",
        "balance-products": "balances",
        "topology": "events",
    }.get(resource, resource)


# These records have no verified warehouse attribution. Never substitute the
# containing venue's data for a selected warehouse's data.
WAREHOUSE_UNSUPPORTED_SECTIONS = frozenset(
    {
        "products",
        "charts",
        "cash-shifts",
        "employees",
        "events",
        "status",
        "deposits",
        *FINANCE_SECTIONS,
    }
)


def require_warehouse_section(scope, section):
    if not scope.warehouse_restricted:
        return
    if section in WAREHOUSE_UNSUPPORTED_SECTIONS:
        raise HTTPException(
            403, "Раздел недоступен при ограничении по складам: нет складской привязки данных."
        )
    if not scope.store_ids:
        raise HTTPException(403, "Нет доступных складов.")


def warehouse_capabilities(scope):
    assigned = sections_for(scope.user)
    unsupported = (
        sorted(set(assigned) & WAREHOUSE_UNSUPPORTED_SECTIONS) if scope.warehouse_restricted else []
    )
    return {
        "supported_sections": [section for section in assigned if section not in unsupported],
        "unsupported_sections": unsupported,
    }
