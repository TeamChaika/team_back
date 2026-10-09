from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.saas_admin.entitlements import evaluate_feature, subscription_state
from app.saas_admin.models import Subscription

NOW = datetime(2026, 10, 8, 22, 30, tzinfo=UTC)

def company(**sub):
    return {
        "status": "active",
        "modules": {
            "analytics": True,
            "documents": True,
            "commercial_invoices": True,
            "deposits": True,
        },
        "subscription": {
            "policy": "plans_v1",
            "plan_id": "full",
            "start_date": "2026-10-01",
            "end_date": "2026-10-08",
            "timezone": "Europe/Simferopol",
            **sub,
        },
    }


def decision(c, feature='analytics.sales', **kwargs):
    return evaluate_feature(
        c, feature, now=NOW, staff_allowed=True, warehouse_allowed=True, **kwargs
    )


def test_expiration_uses_company_timezone_and_history_remains():
    c = company()
    assert subscription_state(c['subscription'], NOW) == 'expired'
    assert not decision(c).allowed
    assert decision(c, operation='history').allowed
    c['subscription']['timezone'] = 'UTC'
    assert decision(c).allowed


def test_override_expires_and_never_bypasses_module_or_scope():
    c = company(
        plan_id="analytics",
        end_date=None,
        overrides={"commercial.outgoing": {"mode": "allow", "expires_at": "2026-10-08T23:00:00Z"}},
    )
    assert decision(c, 'commercial.outgoing').allowed
    c['modules']['commercial_invoices'] = False
    assert not decision(c, 'commercial.outgoing').allowed
    c['modules']['commercial_invoices'] = True
    c['subscription']['overrides']['commercial.outgoing']['expires_at'] = '2026-10-08T22:30:00Z'
    assert not decision(c, 'commercial.outgoing').allowed
    assert not evaluate_feature(
        c, "analytics.sales", staff_allowed=True, warehouse_allowed=False, now=NOW
    ).allowed


def test_deny_override_and_unknown_plan_fail_closed_even_with_allow():
    c = company(end_date=None, overrides={'analytics.sales': {'mode':'deny'}})
    assert not decision(c).allowed
    c['subscription']['plan_id'] = 'made-up'
    c['subscription']['overrides']['analytics.sales']['mode'] = 'allow'
    assert not decision(c).allowed
    assert not decision(company(), 'unknown').allowed


def test_reconciliation_requires_persisted_operation_and_known_feature():
    c = company(status='cancelled')
    c['modules']['deposits'] = False
    assert decision(c, 'payments.status', operation='reconcile', initiated_operation=True).allowed
    assert not decision(c, 'payments.create', operation='create', initiated_operation=True).allowed
    assert not decision(c, 'payments.status', operation='reconcile').allowed
    assert not decision(
        c, "assistant.chat", operation="reconcile", initiated_operation=True
    ).allowed


def test_legacy_retains_existing_access_but_module_false_still_denies():
    c = company(policy='legacy', plan_id=None, plan='Old free text')
    assert decision(c).allowed
    c['modules']['analytics'] = False
    assert not decision(c).allowed


@pytest.mark.parametrize('data', [
    {'policy': 'plans_v1', 'plan_id': 'unknown'},
    {'overrides': {'unknown': {'mode':'allow'}}},
    {'timezone':'not/timezone'},
    {'policy':'plans_v1','plan_id':'full','overrides':{'analytics.sales':{'mode':'allow','expires_at':'2026-10-08T12:00:00'}}},
])
def test_contract_rejects_invalid_policy_data(data):
    with pytest.raises(ValidationError):
        Subscription.model_validate(data)
