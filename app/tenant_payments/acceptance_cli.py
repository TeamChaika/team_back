"""Explicit owner-confirmed payment acceptance; no fleet or HTTP auto-creation."""

import argparse
import asyncio
import json
from uuid import UUID

from app.saas_admin.runtime_operator import RuntimeOperator, private_json
from app.tenancy.payment_bootstrap import build_payments
from app.tenant_payments.acceptance import OwnerAcceptanceAuthority, OwnerPaymentAcceptance


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True)
    actions = result.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="Confirm and create exactly one saved attempt")
    create.add_argument("--mode", choices=("sandbox", "live"), required=True)
    create.add_argument("--terminal", type=UUID, required=True)
    create.add_argument(
        "--amount-minor", type=int, required=True, help="Kopecks, whole rubles only"
    )
    create.add_argument("--request-id", type=UUID, required=True, help="Reuse on retry")
    create.add_argument("--confirm-create", action="store_true", required=True)
    reconcile = actions.add_parser("reconcile", help="Check the existing operation; never create")
    reconcile.add_argument("--intent-id", type=UUID, required=True)
    return result


def main():
    args = parser().parse_args()
    operator = None
    try:
        operator = RuntimeOperator(private_json(args.config))
        service = build_payments(
            operator.runtime,
            environ={"RESTCONTROL_TENANT_PAYMENTS_DATABASE_URL": operator.config["payments_dsn"]},
        )
        acceptance = OwnerPaymentAcceptance(service, OwnerAcceptanceAuthority(operator))
        if args.action == "create":
            intent = acceptance.create_intent(
                terminal_id=args.terminal,
                amount_minor=args.amount_minor,
                mode=args.mode,
                request_id=args.request_id,
                confirmed=args.confirm_create,
            )
            # Durable identifier is printed before transport so recovery never needs a new POST.
            print(
                json.dumps(
                    {
                        "intent_id": intent["id"],
                        "request_id": intent["request_id"],
                        "company_id": intent["company_id"],
                        "mode": intent["mode"],
                        "amount_minor": intent["amount_minor"],
                    }
                ),
                flush=True,
            )
            result = asyncio.run(acceptance.execute(UUID(intent["id"])))
        else:
            result = asyncio.run(acceptance.reconcile(args.intent_id))
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        # DSNs, bearer handles, provider credentials and raw upstream errors stay private.
        print(json.dumps({"completed": False, "error_code": "payment_acceptance_failed"}))
        return 1
    finally:
        if operator:
            operator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
