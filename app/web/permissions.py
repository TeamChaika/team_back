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
    "outgoing": "Расходные накладные",
    "transfers": "Перемещения",
    "writeoffs": "Списания",
    "products": "Номенклатура",
    "charts": "Технологические карты",
    "balances": "Остатки на складах",
    "employees": "Сотрудники iiko",
    "events": "События заказов",
    "status": "Статус данных",
}
IIKO_SECTIONS = set(SECTIONS) - {"deposits"}
DOCUMENT_SECTIONS = {"transfers", "writeoffs"}
ANALYTICS_SECTIONS = IIKO_SECTIONS - DOCUMENT_SECTIONS


def sections_for(user):
    # Older in-process integrations may still supply the legacy profile shape.
    if "sections" not in user:
        return ["deposits"] if user["role"] == "deposits" else list(SECTIONS)
    return [key for key in user["sections"] if key in SECTIONS]


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
