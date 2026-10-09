"""Optional dedicated reconciliation process; no payment creation on worker startup."""

import asyncio

from app.tenancy.config import load_runtime
from app.tenancy.payment_bootstrap import build_payments


async def run():
    runtime = load_runtime()
    if runtime.mode != "tenant":
        raise ValueError("Payment worker requires explicit tenant runtime")
    service = build_payments(runtime)
    while True:
        try:
            await service.reconcile_due()
        except Exception:
            # A failed check retains its persisted lease/operation and will be retried.
            # Do not print exceptions: a DB driver may include confidential connection data.
            print("Tenant payment reconciliation unavailable", flush=True)
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(run())
