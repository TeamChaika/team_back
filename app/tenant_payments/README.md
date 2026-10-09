# Tenant deposits adapter

New company-owned implementation of the existing dashboard deposits contract.
Legacy `paychaikatema` is unchanged. The runtime is immutable and the database
connection uses **`c_<company UUID hex>_payments_runtime`**, with data privileges
only in its own payments schema. Ordinary analytics/runtime login is rejected.
The migration operator installs a password separately; no password is generated
or embedded in this migration.

## Bootstrap contract

```python
store = PaymentStore(runtime, payments_dsn, company_vault)
service = TenantPayments(store, documented_provider)
router = create_router(
    service,
    principal_dependency,
    check_origin,
    company_users=repository.deposit_users,
    validate_user=company_user_is_active,
)
```

`principal_dependency` must construct `PaymentPrincipal` only from the common
portal's freshly verified company session/scope and typed `ActorContext`.
Members require active deposits section, separate deposit venue grants and no
unsupported warehouse-only scope. Global platform owner is fully administrative
inside the selected verified company; no synthetic membership/profile is created.
`validate_user` must check a grant recipient belongs to this company and is active.
`check_origin` is the common authenticated portal Origin/CSRF gate, not a replacement
login or a legacy pay token relay.

`GET /api/deposits`, `/export`, `/permissions`, `/venues`, `/creation-venues`,
`/access`, `/{id}` and POST `/api/deposits`, `/access/{grant,update,revoke}`
retain the existing UI response shapes. `/venue-options` adds `{id,name}` rows;
UI can move selectors to UUID without changing their displayed labels. A supplied
legacy name is resolved only inside this company before storage of its UUID.
There are no shared-name joins or shared RLS grants.

`PATCH /api/deposits/{id}` takes NewDeposit fields and `revision`; it is available
before any payment intent. Once a provider attempt exists, amount/booking edits
are blocked rather than silently changing an issued operation. Return `guest_url`
from saved create/detail results; the shared frontend must use it, not the old
hardcoded pay.chaika.team link.

`GET /api/payment-settings` returns management-compatible venues/terminals without
keys; POST `/venues/{venue_id}` and `/venues/{venue_id}/terminals/{terminal_id}`
under this prefix save config with revision checks. The existing management router
may call `store.management/save_venue/save_terminal` directly to preserve its URLs.
Terminal mutations create immutable encrypted versions. Attempts pin their own
version, key, amount and currency, including after default-terminal changes.

## Guest and provider lifecycle

- Link: `<company frontend>/deposit/<deposit UUID>?token=<random capability>`.
  Capability is encrypted at rest plus a comparison hash; it is not an Auth JWT.
- GET `/api/guest-deposits/{id}?token=...` reads public money/booking/status and
  optional `payment: {state,payment_url,qr_image,diagnostic,valid_until}`. It omits
  staff guest phone/name/notes.
- POST `/api/guest-deposits/{id}/prepare` body `{token,request_id}` requires exact
  company frontend Origin. Persisted attempt and callback token precede external
  POST. Parallel calls and changed request IDs cannot bypass pending/unknown intent.
- POST `/api/guest-deposits/{id}/reconcile` same body/Origin only refreshes saved
  state; it never creates another QR.
- POST `/api/payment-callbacks/{attempt UUID}?token=...` accepts JSON containing
  `operation_id`. Token admits the callback; caller status does not mark paid.
  The server independently queries provider, compares operation/amount/currency
  and configured merchant, then atomically settles attempt+deposit. Callback
  arriving before creation response is supported. Repeat/out-of-order results
  preserve confirmed paid and append one payment-paid audit event.
- Payment processor or reverse proxy must suppress capability/callback tokens
  from access logs and use no-store responses. Common portal host/CORS gates apply.

`TenantPayments.reconcile_due()` processes leased attempts after missed callbacks
or restart. The common scheduler can call it, or an independently supervised
`python -m app.tenant_payments` process uses `RESTCONTROL_TENANT_PAYMENTS_DATABASE_URL`
and the company runtime config. API/worker must share the same private company
vault (runtime `payments-vault/credentials.key`), with backup/restore alongside
the payment schema. Worker never creates payments. A creation result without
operation ID remains `unknown`: current provider has no documented search by local
request key, so no automatic create retry is safe.

## Actual readiness limitation

See [PROVIDER.md](PROVIDER.md). Current official QR Manager Swagger publishes
the sandbox host. This adapter separately pins the live host from the existing
production integration. Both modes are explicit and supported. Authenticated key/terminal
checks precede creation. Expanded provider-returned SBP QR currency + server
creation UUID + versioned merchant context can settle documented SSE statuses.
Unified QR links carry no currency; these remain `currency_unverified` unless a
provider status explicitly supplies valid currency. See PROVIDER.md for the
current contract and NSD format transition.

Run `tests/test_tenant_payments_postgres.py` with explicit loopback disposable
`RESTCONTROL_PAYMENTS_TEST_DSN`, plus `tests/test_tenant_payment_provider.py`.

The full portal factory mounts this service at the existing deposit/management UI routes and starts bounded reconciliation. Staff access is reconstructed from a fresh verified ActorContext, active company membership/profile and current profile revision. Account access changes commit the analytics profile before payment grants; an interrupted second write fails closed through the profile revision mismatch and can be repaired by saving the employee again. Payments SQL always uses the separate payment DSN.

Management can POST `/api/payment-settings/venues/{venue_id}/terminals/{terminal_id}/validate` to check the saved key without creating an operation. The common management UI has a tenant-only terminal-check button. Evidence migration `20261009210000_tenant_payment_evidence.sql` is required.
