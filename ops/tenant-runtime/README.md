# Private tenant processes — explicit local operator workflow

This tooling is not production rollout approval and does not mark incomplete modules
ready. Use a reviewed operator manifest outside every tenant directory, mode 0600.
No production invocation was made while implementing/testing this workflow.

Prerequisite: apply the reviewed control-plane migration
`20261008235000_restcontrol_runtime_provisioning.sql` through the normal release
process. The operator does not self-upgrade the central registry schema.

Required operator JSON fields:

- `company_id`, `registry_data_directory`, `registry_dsn`, `operator_dsn`
- `runtime_root`, `runtime_dsn`, `payments_dsn`
- `auth_url`, `auth_anon_key`, `verifier_socket`
- `history_from`, `history_to` (explicit inclusive initial period)
- for `serve-verifier` only: `auth_admin_key`, `verifier_grants`, an array of
  `{company_id, role, secret_file}`; roles are `portal`, `documents-worker`,
  `payments-worker`. Use different random capabilities per company and role.

All four DSNs must explicitly identify the same database host/port/name. Runtime
and payments DSNs must use their UUID-derived restricted logins; each needs its own
password. The operator DSN is used only for DDL/role provisioning. Registry DSN and
Auth administrator key never enter a tenant environment. Store original registry
Fernet key plus per-company payment vault keys in the backup plan.

Commands from backend with its virtual environment:

```text
python -m app.saas_admin.runtime_operator --config /private/operator.json enqueue
python -m app.saas_admin.runtime_operator --config /private/operator.json serve-verifier
python -m app.saas_admin.runtime_operator --config /private/operator.json run
python -m app.saas_admin.runtime_operator --config /private/operator.json status
python -m app.saas_admin.runtime_operator --config /private/operator.json work
```

Run the verifier separately under a supervisor before `run`. `enqueue` snapshots
explicit current company/version plus saved encrypted Chain/RMS credentials into
its private manifest. `run` commits successful real stages and resumes failed stages
on retry. Initial synchronization calls the existing common collector/sync modules,
not bespoke analytics code. The collector and sync environment omit payment DSN and
verifier capability. Portal gets only its own analytics/documents role, separate
payments role and company-pinned verifier capability. Output contains no credentials.

`start-portal` and `start-collector` launch private socket processes, record private
PID metadata and refuse duplicate/mismatched sockets. They do not make readiness true.
Foreground portal command for systemd:

```text
python -m app.tenancy.bootstrap --config /private/tenants/c_UUIDHEX/environment.json
```

Service users/directories/UDS ACLs must permit only the gateway/verifier and the
specific company processes. Do not run unrelated tenant users under a shared Unix
account in a hardened deployment. Existing socket/PID records are not permission
to kill an unrelated process. Fleet supervision tracks its own process groups and
verifies private health process IDs before removing their sockets. Stop processes through their configured supervisor;
never remove a live socket blindly. Gateway and verifier HTTP access logs must stay
disabled because payment URLs contain capabilities.

`modules` now performs real read-only HTTP acceptance over the company's private
portal using `acceptance_session_file` (0600, an existing current platform-owner
SSO session). It verifies own identity and enabled-feature lists, filters and CSV/XLSX
exports, checks worker/scheduler heartbeats when their capabilities are enabled,
and verifies a configured Telegram bot with read-only getMe. No acceptance identity
is minted and no document/payment/provider write is submitted by these probes.
Missing sessions/settings/services produce concrete sanitized stage codes and
failed check evidence; after configuration, explicit retry resumes that stage.
Assistant acceptance checks its configured status, not a paid inference request.

The payments stage is not mandatory for a plan with payments.create disabled.
When enabled it reads own active terminal configuration under the payments role.
Missing terminal yields `payment_terminal_required`; an absent provider-validated
current terminal check yields `payment_terminal_check_required`; missing durable
settlement evidence pinned to that credential version yields
`payment_settlement_proof_required`. Existing provider-confirmed paid attempts with
verified amount, currency and merchant binding can satisfy this check. The adapter
never initiates a payment or invents evidence. Sandbox proof remains labeled sandbox
and does not establish live payment acceptance.

Local test `test_full_portal_processes.py` launches two actual portal subprocesses,
real tenant baseline schemas/DB roles and a private central verifier. Central SSO,
member sessions, products/documents and cross-company denial are exercised. Only
Auth's external HTTP provider is synthetic. The test registry routes the two running
test processes explicitly and is not a production readiness assertion.

`work` is the supervised consumer for owner-enqueued requests. It polls only the
configured company every three seconds and resumes pending/interrupted work through
the durable stage runner; `work-once` performs one bounded poll. Render failures
become sanitized failed status and require explicit retry. Configuration changes
invalidate pending old-version work. Previously accepted runtime bindings remain
available only for existing payment reconciliation and account recovery, while new
creation and full readiness require current configuration acceptance.

The verifier additionally requires `identity_dsn`, or `identity_targets` entries
with `company_id`, `identity_dsn` and optionally a private `environment_file` for
another company. Each identity DSN must use `c_UUIDHEX_identity_runtime` in the same
database. The actual verifier factory constructs CompanyAccounts from these trusted
targets; identity credentials remain central and are never rendered into children.
Pass that same CompanyAccounts instance to the central gateway factory's
`company_accounts` argument so its password endpoint clears both identity flags.

Central supervised gateway assembly (same operator JSON as the verifier):

```text
python -m app.saas_admin serve --mode production --origin https://admin.example.org --data-dir /private/registry --dist-dir /srv/frontend --uds /private/sockets/gateway.sock --runtime-config /private/operator.json
python -m app.saas_admin.runtime_operator --config /private/operator.json run-documents-worker
python -m app.saas_admin.runtime_operator --config /private/operator.json run-scheduler
```

`--runtime-config` is explicit opt-in; without it the gateway stays limited. The
factory validates registry directory identity, initializes CompanyAccounts using
trusted identity targets, and installs runtime registry/provisioning endpoints.
Operator JSON must have both exact `public_dns_targets.frontend` and `.api`, each
`{type: A|AAAA|CNAME, value: ...}`. No DNS records or certificates are created by CLI.

Private optional settings use an explicit whitelist:
- `document_settings`: worker_enabled, commercial_enabled, commercial_submit_enabled,
  commercial_counterparty_create_enabled, commercial_seller_json (object/string),
  bot_token and bot_username (without @).
- `collector_settings`: sync_enabled, live_sales_enabled and sync_api_key.
- `assistant_settings`: provider, api_key, timeweb_agent_id, model,
  requests_per_hour and max_output_tokens.

Render after changing operator settings, then restart affected processes through
their supervisor. A documents worker requires its own documents-worker capability
in verifier_grants and worker_enabled=true; it receives no payments/assistant keys.
Scheduler receives only own collector settings and no document/verifier/payment keys.
The tenant scheduler uses the separately supervised collector and verifies its own
company/version health; it never terminates that collector. Existing live unknown
sockets are not adopted or removed by the fleet supervisor. Live sales uses the portal's own iiko credentials and explicit
collector_settings.live_sales_enabled, with no fallback to any global environment.

Lifecycle: apply control migration; configure own DSNs/capabilities/settings; render;
start verifier and central gateway; enqueue/run initial stages; supervise the tenant
portal, document worker and scheduler as configured; provide current owner acceptance
session; retry missing-configuration stages; publish verified own DNS/TLS and retry.
All acceptance stages must pass durably before full dashboard readiness is exposed.
Capability/role files, vaults and operator manifests must remain outside static roots.

History availability is separate from deployment readiness: context and authenticated
workspace expose `full_dashboard_available` for the last accepted active binding.
Safe GET/history and exact known read-only report POSTs use it with current plan
permissions; new mutations still require current-version readiness. Suspended or
archived companies do not retain an accepted route.

Recovery gateway throttles by company and trusted transport peer (10 / 5 minutes).
It ignores raw X-Forwarded-For. A request without a transport peer fails closed.
For UDS, configure edge_token_file (0600) and optionally exact edge_trusted_peers.
The gateway accepts X-RestControl-Client-IP only with a constant-time verified
X-RestControl-Proxy-Token from UDS or an explicitly trusted transport peer. Caddy
must overwrite both headers as shown in [edge.caddy](edge.caddy), using its actual
remote_host and the same privately loaded token. Raw X-Forwarded-For remains ignored.

Mechanical fleet preparation:

```text
python -m app.saas_admin.runtime_fleet --config /private/fleet.json
```

Fleet JSON contains `operator_directory` (0700 outside tenant roots) and
`operator_template`: shared platform connection/registry/auth/runtime-root/verifier
settings used by the normal operator. No company ID, per-company role DSNs, identity
maps, acceptance token or company document/assistant/collector credentials belong in
this shared template. Pending companies generate stable UUID role passwords and
separate private portal/document-worker capabilities. Repeating preparation retains
credentials; it neither rotates roles nor submits jobs or external writes.

One-time platform prerequisites: control schema and operator/registry DSNs, Auth
project/admin credentials, private filesystem/service identities, trusted edge/DNS
configuration and backup policy. Configure the platform template with
`acceptance_root` (0700, outside runtime roots), `edge_token_file`, `verifier_socket`,
`public_dns_targets`, `history_from` and `history_to`, and the shared Auth/DB fields.

Preferred automatic fleet lifecycle:

```text
python -m app.saas_admin.runtime_fleet --config /private/fleet.json serve-verifier
python -m app.saas_admin.runtime_fleet --config /private/fleet.json work
python -m app.saas_admin serve --mode production --origin https://admin.example.org --data-dir /private/registry --dist-dir /srv/frontend --uds /private/sockets/gateway.sock --runtime-config /private/fleet.json
```

The fleet gateway needs no anchor company. Its private verifier and CompanyAccounts
lazily discover validated UUID-named 0600 company operator files on each use; new
companies do not require a verifier/gateway restart or manual identity target edits.
Launch/retry uses the logged-in owner's existing central session through normal
server-side PKCE authorization/exchange. Only the company-bound opaque child is
written to central acceptance storage. Parent cookies are never written to files.
Before module acceptance, the existing child is renewed only after checking its
live parent, current company/origins/version and the running modules job. Renewal
is capped by parent expiry and 30 minutes. Parent logout/revocation remains binding;
if the parent has expired, the owner retries from the panel without exporting tokens.

Fleet work prepares pending UUID credentials, supervises each company's portal,
collector, document worker and scheduler, and resumes process-start/heartbeat waits.
It refreshes module-derived settings without rotating passwords and stops its own
process groups on archive/version replacement (including in-progress sync children).
Stale sockets are removed only after refusal or confirmed owned process shutdown;
live unknown sockets are never killed or adopted. The supervisor must remain under
an OS service manager; [restcontrol-fleet.service](restcontrol-fleet.service) is a
reviewable template, not an installed service.

Per-company external inputs remain saved own iiko credentials, desired module/seller/
bot/assistant/terminal settings and domain ownership/DNS. These are business/provider
configuration, not manually generated DSNs, grants or acceptance-session exports.
Payment terminal/settlement evidence and every other durable readiness check remain
mandatory when the corresponding capability is enabled. No real deployment, provider
payment or document submission was performed while implementing this automation.
