# QR Manager transport contract

Official specification checked **2026-10-09**:
https://docs.qrmanager.ru/static/merged_swagger-2.json (API v2.0), linked from
https://docs.qrmanager.ru/docs/v2.

- Published sandbox origin: `https://app.devwapiserv.qrm.ooo`. The specification
  says production host must be obtained from `post@qrm.ooo`. This adapter cannot
  accept an arbitrary URL. Live is explicitly pinned to `https://app.wapiserv.qrm.ooo`
  from the existing deployed `paychaikatema/backend/app/services/qr_manager.py`;
  this provenance is operator configuration, not a claim Swagger publishes live.
- `POST /operations/qr-code/`, required `X-Api-Key`, body `sum` positive integer
  kopecks, `notification_url`, `redirect_url`. Local caller accepts RUB only;
  the operation schema itself does **not** establish a currency field.
- Response `results.operation_id` UUID, `payment_page_link` (fallback `qr_link`),
  optional `qr_img`. No documented absolute validity timestamp; none is invented.
- QR creation has no documented request-id/idempotency header. `request_id` is
  only the caller's persisted local identifier and is not sent to the provider.
  Save/reserve the attempt before create. Timeout, non-success unknown status,
  invalid success response, and redirects are `CreationUnknown`. Never redispatch
  an unknown attempt automatically. Explicit client rejection is `CreationRejected`.
- `GET /api/v2/sse-operations/{id}/qr-status/` returns SSE: complete blank-line
  delimited `data:` frames. Streaming has a total deadline and decoded-byte cap,
  including comments/keepalives. Connections close when a result/error is obtained.
  JSON response framing is supported as shown by the Swagger response schema.
- `results.operation_sum` is positive integer kopecks. Codes 3/4 pending,
  5 observed paid, 6/8 failed. Code 0 means wait expired and is **unavailable**,
  never failed. Malformed/missing amounts and unknown codes fail closed.
- SSE schema exposes no currency, merchant, operation-id echo, or paid-at.
  `operation_id` in the result is the UUID bound into the fixed origin/path,
  and missing fields remain `None`. **An observed paid status cannot settle an
  invoice when mandatory currency/merchant matching is unavailable.** No currency
  is copied from the invoice to make verification appear successful.
  If the provider actually supplies a `currency` field, only an uppercase
  three-letter code is accepted. An optional `operation_id` echo must match the
  requested UUID. Neither extension is assumed to exist in the documented API.
- SSE method documents no authentication parameter. Adapter sends `X-Api-Key`
  as defense in depth, but does not assert that the provider authenticates it.
  Callback token is local admission control, not a QR Manager signature. Callback
  data and browser return URLs never prove payment; server check is required.
- No raw body, provider error details, credential, or callback token is returned
  by the adapter. Outbound API requests use the selected fixed environment origin, no redirects
  or environment proxy settings. Returned HTTPS payment/media URLs are never fetched.

Validation uses mock transport only: no public demo key, customer key, live
endpoint, actual creation or actual payment was used. Sandbox is documented to
auto-pay most operations after 15 seconds; that would not prove real settlement.

## Authenticated terminal and creation evidence

`GET /users/check-api-key/` is authenticated with the exact pinned version's API
key. Its merchant_id, qrt_name, subscription_end_date and B2C/fiscal flags are
persisted in append-only terminal_checks; each attempt FK binds that check to
the same immutable terminal version. Explicit configured merchant mismatch
rejects creation. Creation is enabled only with a future subscription end date,
B2C and explicitly false requires_receipt/is_nomenclature/is_cash_link: the
current deposit adapter does not submit a receipt basket. Missing flags fail
closed. Checking a terminal itself is read-only at the provider, not a payment.

Merchant context is not invented in SSE: an authenticated key check establishes
the merchant of the exact credential used for the authenticated creation response.
Only after that response confirms the same operation UUID can its persisted
merchant context be used with subsequent UUID-bound status checks. An early
callback does not establish this creation provenance.

Currency may be verified from an **actual provider-returned expanded dynamic SBP
qr_link**: exact HTTPS qr.nspk.ru host, 32-character QR identifier, unique type=02,
bank (12 digits), sum (matching stored minor units), cur=RUB and CRC syntax.
It is persisted separately as creation_currency/provider_amount_minor/QR payload,
never copied from the invoice. CRC is format-checked, not a provider signature;
provenance is the TLS authenticated creation response that also returns UUID.
An explicit current currency or merchant mismatch cannot be bypassed by context.

[NSD's official 18 August 2026 notice](https://www.nsd.ru/ru/news/izmenenie-formata-funktsionalnoj-ssylki-s2v-s-rasshirennoj-na-unifitsirovannuyu/)
confirms both expanded currency/sum semantics and the transition to unified
no-query links from 1 September 2026. **Unified links lack this currency evidence**;
settlement remains blocked with currency_unverified unless provider status itself
supplies an explicit valid currency. A separate authenticated operation detail
and documented currency_id mapping, or provider contract confirmation, is needed
to unblock those operations. This is a concrete contract limit, not a permanent
sandbox-only implementation. No QR link is fetched to invent missing fields.
