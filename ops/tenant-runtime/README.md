# Private tenant processes — explicit local operator workflow

This tooling is not production rollout approval and does not mark incomplete modules
ready. Use a reviewed operator manifest outside every tenant directory: Linux root:central
0640, or explicit local-test 0600 (see the process identity section below).
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
Operator JSON uses `public_dns_targets.edge` with
`{type: A|AAAA|CNAME, value: ...}` pointing at the public gateway. Company cards
show one record for the registered main domain, never a required API subdomain.
Existing manifests with the complete `frontend`/`api` pair remain supported:
`api` supplies the gateway target and the old static `frontend` target is ignored
for the displayed DNS instructions. Partial/mixed configurations fail closed.
Instructions are recalculated on each status read without changing saved job
states or readiness evidence. No DNS records or certificates are created by CLI.

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
All nine acceptance stages must pass durably before full dashboard readiness is exposed.
A PendingCheck for optional modules/payments preserves its failed evidence and continues
independent DNS/TLS and runtime-health checks. Unexpected errors or failed prerequisites
still stop processing and discard the failed stage's old partial evidence.

`working_dashboard_available` requires current-version migrations, database_roles,
identity, connections, initial_sync, dns_tls and runtime_health. It does not mark the
company ready or establish an accepted historical binding. `feature_readiness` maps
all 28 known feature IDs to `{state, read, write, reasons}` using current company/version
module probes and explicit service evidence. Missing own Telegram, AI, commercial seller
or payment settlement proof only closes the corresponding features. Finance has no
tenant schema/catalog capability and remains unavailable. Unknown evidence fails closed.
Gateway and private verifier enforce writes separately; portal actor/warehouse ACLs remain.
The owner retains the exact setup routes even when working modules are incomplete.

Terminal/default-terminal changes take the same company provisioning advisory lock,
revoke payment proof durably before forwarding, and hold the lock through the private
portal response. Busy provisioning returns 503; a changed company version returns 409.
On upgrade, explicitly enqueue/retry previously accepted companies: the provisioner
rechecks legacy module evidence without company/version/services and payment proofs
without terminal_version_id while retaining compatible schema/sync evidence.
Failed terminal writes keep the revocation. Retry real acceptance before creating a new
payment; reconciliation of existing persisted operations remains available.
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
lazily discover validated UUID-named company operator files on each use; new
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
If a supervised socket is bound but its health request cannot connect or times out,
the operator records `runtime_process_pending`; the existing fleet retry waits
30 seconds initially and 60 seconds between later attempts. Each health request
keeps its 5-second timeout. An HTTP error, malformed response or company/version
mismatch remains a hard failure; no socket is removed or process duplicated.
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

### Linux process identity boundary

Production fleet configuration must now include in `operator_template`:

```json
"process_isolation": {"mode": "linux", "central_group": "restcontrol-saas"}
```

There is no implicit shared-user fallback. `local-test` is an explicit alternative
for disposable tests only; a production gateway rejects that policy. The supervised
fleet/operator CLI runs as root, creates dedicated system accounts named
`rc_<full UUID encoded in base32>` (29 characters), and launches business processes
with that company's own UID/GID and no supplementary groups. It does not add tenant
accounts to the central group. Only trusted operator CLI operations create accounts;
the HTTP gateway never creates them. `NoNewPrivileges` is set before Linux child
launch, including initial synchronization. Direct CLI exec drops all UID/GID slots
and closes inherited file descriptors. Existing accounts with unexpected home,
shell, UID sharing or group memberships are rejected rather than modified.

The trusted release and Python interpreter, and their ancestors, must be root-owned
and not writable by group/others. Tenant processes start in their own directory;
`PYTHONPATH` points only at the trusted release. Use these distinct filesystem rules:

| Path | Ownership and mode | Purpose |
| --- | --- | --- |
| Fleet JSON and per-company operator JSON | `root:restcontrol-saas`, `0640` | Central read access, no gateway modification of root launch settings |
| Operator directory | `root:restcontrol-saas`, `0750` | Outside tenant/static roots; central capability originals also live here |
| Central capability originals `c_UUID.verifier.key`, `c_UUID.documents-worker.key` | `root:restcontrol-saas`, `0640` | Verifier authority; never read back from tenant copies |
| Tenant root | `root:root`, `0711` | Traverse to own known company directory; cannot list/write |
| Company directory `c_UUID` | `companyUID:restcontrol-saas`, `02750` | Own company writes, central gateway traversal, no access by another company |
| Environment and company capability copies | `companyUID:companyGID`, `0600` | Only that company's processes and the root operator |
| Portal/collector sockets | `companyUID:restcontrol-saas`, `0660` | Bound before Uvicorn with explicit mode; central gateway can connect |
| Verifier socket directory | verifier service UID, `0711` | Separate directory containing only the socket; all ancestors root-owned and non-writable by others |
| Verifier socket | verifier service UID, `0666` | Local capability-authenticated endpoint; exact company and role checks remain mandatory |
| Registry, acceptance storage, encryption keys | Existing central service UID, `0700` directories / `0600` files | Never inside the traverse-only verifier or tenant roots |

The gateway and verifier use the dedicated central group and remain unprivileged;
no `CAP_DAC_OVERRIDE` is required. The verifier socket directory must be owned by
its actual service UID; the root fleet must not place secret files in it. The fleet
JSON and all central directories must be prepared by the operator with the stated
ownership before gateway/verifier startup. The root fleet prepares company accounts,
directories and immutable central originals; group modes are accepted only by narrow
operator loaders, not by the generic private-JSON reader. Linux private child reads
use descriptor-pinned, nonblocking, size-limited regular files with exact ownership.
Corrupt tenant files produce a sanitized delayed launch failure for that company;
other companies stay running. Detailed child stdout/stderr remains disabled.

Tenant roles within one company intentionally share that company's Unix account;
this boundary isolates companies and the central control plane, not same-company
workers from each other. The trusted central gateway can traverse tenant directories
and connect sockets but cannot read the `0600` tenant environment or payment vault.
The root operator backs up tenant vaults separately.

Linux acceptance must run in a **disposable root Linux Docker container**, with the
checkout copied root-owned under `/srv/backend`, root-owned Python interpreter,
`useradd`/`userdel`, group `nogroup`, user `nobody`, and full test dependencies installed. This test
refuses ordinary hosts and is skipped unless explicitly enabled:

```text
RESTCONTROL_LINUX_IDENTITY_TESTS=1 python -m pytest tests/test_runtime_process_identity.py -q
```

The Linux test also checks central-group connection to a `0660` company socket
and denial for the other tenant. Verify the actual production gateway service
identity has that same access before enabling the fleet.
A local/macOS pass with this Linux test skipped is not production isolation proof.


Primary administrator initialization: the operator may create a fresh local company
administrator only from an active, exclusive primary `company_admin` membership with
matching Auth UUID/email/provision marker and completed central provisioning journal.
Platform identities are excluded. The fixed execution-only identity routine preserves
all existing profiles on replay; a mismatched receipt, missing employee profile, or
ambiguous binding blocks identity acceptance. The new profile is a company member
(`manager`, portal administrator, own company sections/departments), never a platform
owner. Document warehouse/payment grants are configured separately through existing ACLs.
The central completed-password state is preserved; no Auth creation or reset occurs.
Tenant migration evidence contains a fingerprint of ordered filenames, areas and SQL
checksums. Normal retry rechecks old/missing fingerprints even at the same company version.

### Самостоятельная смена Telegram / ИИ

Tenant integrations используют отдельный `integrations_revision` внутри зашифрованной
центральной записи. Сохранение не меняет `company.version`, историю и проверки
аналитики, платежей, миграций или синхронизации. В одной транзакции с настройкой
`integration_rollout.invalidate_integrations` отзывает только доказательство
изменённого сервиса и сохраняет `checks.integrations` со статусом `pending`.

Fleet каждые три секунды сверяет ревизию, пересобирает приватный environment и
перезапускает только собственные portal/documents-worker; collector/scheduler и
другие компании продолжают работать. Центральные ключи Auth/registry не передаются
в дочерние процессы. Наличие собственного бота включает worker даже при выключенных
документных модулях, поскольку привязка и восстановление аккаунта требуют polling.

После перезапуска проверяется company/version/revision в private health. ИИ получает
только доказательство `AssistantSettings.configured` фактически загруженного процесса;
это **не проверка действительности ключа у провайдера** и не платный запрос к модели.
Telegram проверяется через `getMe`, точное имя бота и чистый heartbeat живого собственного
worker, записанный после его запуска. Здесь не создаются сессии владельца и не
подменяется полноценная первоначальная приёмка: старое доказательство маршрутов и
базовой готовности остаётся обязательным.

`checks.integrations` переживает перезапуск fleet: сетевой сбой или ещё не запущенный
child оставляет `pending` с автоматическим повтором. Неверная идентичность непустого
бота даёт `failed`/`telegram_identity_unverified`; явное удаление применяется как
`applied` с отключённым сервисом. `applied` означает загрузку конфигурации и указанные
узкие проверки, а не отправку сообщения или успешный ответ ИИ. Для новой ревизии
статус предыдущей ревизии не должен отображаться как актуальный.

Для уже проверенной той же версии компании добавление **только**
`20261010110000_tenant_telegram_bot_namespace.sql` выполняется через durable
`migration_refresh`: точный прежний fingerprint должен совпадать с неизменённым
префиксом текущего manifest. Durable-маркер привязан к точному целевому fingerprint;
при изменении manifest после остановки задания маркер удаляется и выполняется
обычная приёмка. Выполняется только миграция; предыдущие module/payment
proof, включая частичную готовность с `ok=false`, и прежние state/step/error
сохраняются дословно. После сбоя повторяется этот же ограниченный этап. Изменённые
SQL, неизвестные миграции и новая версия компании не получают такой режим.
Первоначальная приёмка и изменения identity/прав проходят обычную полную процедуру.
