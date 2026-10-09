"""Server-owned capability policy; never replaces actor or warehouse authorization.

Subscription data is authoritative in restcontrol.companies.body, saved through the
existing versioned/audited metadata write. No client supplied plan catalog is used.
Legacy is explicit on presentation, preserving the previous five module switches.
"""
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

# Stable identifiers describe product capabilities, never routes or employee roles.
FEATURES = {
    'analytics.overview': ('Обзор', 'analytics'),
    'analytics.sales': ('Продажи и расшифровки', 'analytics'),
    'analytics.indicators': ('Показатели', 'analytics'),
    'inventory.catalog': ('Номенклатура и техкарты', 'analytics'),
    'inventory.balances': ('Остатки и поиск', 'analytics'),
    'purchases.prices': ('Закупочные цены', 'analytics'),
    'purchases.impact': ('Влияние закупочных цен', 'analytics'),
    'iiko.history': ('История документов iiko', 'analytics'),
    'iiko.cash_shifts': ('Кассовые смены', 'analytics'),
    'iiko.order_events': ('События заказов', 'analytics'),
    'documents.waybills': ('Расходные накладные', 'documents'),
    'documents.writeoffs': ('Списания', 'documents'),
    'documents.approval': ('Согласование и приёмка', 'documents'),
    'documents.dispatch': ('Очередь отправки документов', 'documents'),
    'commercial.incoming': ('Приход в iiko', 'commercial_invoices'),
    'commercial.outgoing': ('Реализация', 'commercial_invoices'),
    'commercial.counterparties': ('Создание контрагентов', 'commercial_invoices'),
    'commercial.invoice_pdf': ('Счета и PDF', 'commercial_invoices'),
    'employees.management': ('Сотрудники, PIN и карты', None),
    'deposits.bookings': ('Бронирования и депозиты', 'deposits'),
    'payments.create': ('Платёжные ссылки', 'deposits'),
    'payments.status': ('Статусы оплат', 'deposits'),
    'management.users': ('Пользователи и права', None),
    'management.settings': ('Заведения и терминалы', None),
    'profile.account': ('Профиль и восстановление', None),
    'notifications.telegram': ('Telegram', 'documents'),
    'assistant.chat': ('Помощник', None),
    'operations.sync': ('Синхронизации и статусы', None),
}
# This is an initial server-controlled proposal, not a billing integration.
PLANS = {
    "analytics": (
        "Аналитика",
        frozenset(k for k, (_, m) in FEATURES.items() if m == "analytics")
        | {"profile.account", "management.users", "operations.sync"},
    ),
    "operations": (
        "Операции",
        frozenset(
            k for k in FEATURES if not k.startswith(("deposits.", "payments.", "assistant."))
        ),
    ),
    "full": ("Все модули", frozenset(FEATURES)),
}
RECONCILABLE = frozenset(
    {
        "documents.dispatch",
        "commercial.incoming",
        "commercial.outgoing",
        "commercial.counterparties",
        "payments.create",
        "payments.status",
    }
)


def catalog():
    return {
        "version": 1,
        "features": [
            {"id": k, "label": label, "module": module} for k, (label, module) in FEATURES.items()
        ],
        "plans": [
            {"id": k, "label": label, "features": sorted(features)}
            for k, (label, features) in PLANS.items()
        ],
    }


def subscription_state(subscription: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError('Policy clock must be timezone aware')
    today = (
        now.astimezone(ZoneInfo(subscription.get("timezone", "Europe/Simferopol")))
        .date()
        .isoformat()
    )
    if subscription.get('status', 'active') != 'active':
        return subscription['status']
    start, end = subscription.get('start_date'), subscription.get('end_date')
    if not start:
        return 'not_set'
    return 'scheduled' if start > today else 'expired' if end and end < today else 'active'


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def evaluate_feature(
    company: dict,
    feature_id: str,
    *,
    operation: Literal["create", "history", "reconcile"] = "create",
    staff_allowed: bool,
    warehouse_allowed: bool,
    initiated_operation: bool = False,
    now: datetime | None = None,
) -> Decision:
    """Reconcile is trusted server work on a persisted operation, never a new write.

    Callers must derive initiated_operation from stored intent/queue identity, not
    request data. A global owner passes full staff rights, but no purchase bypass.
    """
    if feature_id not in FEATURES or operation not in {'create', 'history', 'reconcile'}:
        return Decision(False, 'unknown_feature_or_operation')
    if not staff_allowed or not warehouse_allowed:
        return Decision(False, 'actor_scope')
    if operation == 'reconcile':
        return Decision(
            initiated_operation and feature_id in RECONCILABLE,
            "persisted_operation"
            if initiated_operation and feature_id in RECONCILABLE
            else "not_reconciliation",
        )
    sub = company.get('subscription', {})
    policy = sub.get('policy', 'legacy')
    if policy not in {'legacy', 'plans_v1'}:
        return Decision(False, 'unknown_policy')
    if policy == 'plans_v1' and sub.get('plan_id') not in PLANS:
        return Decision(False, 'unknown_plan')
    module = FEATURES[feature_id][1]
    if module and company.get('modules', {}).get(module) is not True:
        return Decision(False, 'module_disabled')
    if policy == 'legacy':
        return Decision(True, 'legacy_modules')
    now = now or datetime.now(UTC)
    override = sub.get('overrides', {}).get(feature_id, {})
    expires = override.get('expires_at')
    if expires and datetime.fromisoformat(expires.replace('Z', '+00:00')) <= now:
        override = {}
    mode = override.get('mode', 'inherit')
    purchased = mode == 'allow' or (mode == 'inherit' and feature_id in PLANS[sub['plan_id']][1])
    if not purchased:
        return Decision(False, 'feature_disabled')
    if operation == 'history':
        return Decision(True, 'history')
    if company.get('status') != 'active' or company.get('archived_at'):
        return Decision(False, 'company_inactive')
    return Decision(
        subscription_state(sub, now) == "active", "subscription_" + subscription_state(sub, now)
    )
