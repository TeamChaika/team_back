# Dedicated RestControl deployment

Updated 2026-10-08. The dedicated VPS `5.42.103.76` runs `app.saas_admin`.
The owner SPA/API remain same-origin at `https://rc.chaika.team`, with the existing
Supabase PostgreSQL `restcontrol` registry and Supabase Auth.

Tenant and optional payment domains now use the same-origin edge described below:
DNS points to this VPS, `/api` reaches the private gateway, and static frontend
bytes come from the existing Timeweb App `254029` technical HTTPS host. The App
continues to own frontend builds/deployments. Legacy `api.<domain>` routes remain
for compatibility; no new API DNS is needed. Host-only Secure SameSite=Strict
cookies, exact registry routing, CSRF and tenant permissions remain mandatory.

Historical limited Overview/Sales details are in
[the original slice contract](../../docs/tenant-dashboard.md); they do not describe
the current full tenant runtime or establish production/browser acceptance.

## Layout

- `/opt/restcontrol-saas/releases/<backend-sha>-<frontend-sha>/`: immutable source
  (`backend/app/saas_admin`, empty `app/__init__.py`, and pure helpers
  `app/web/{__init__,overview,coverage}.py`) and built `frontend/`.
- `/opt/restcontrol-saas/current`: symlink to the deployed release.
- `/opt/restcontrol-saas/venv`: runtime from `requirements-saas-admin.txt`, including
  `defusedxml==0.7.1`. The reused overview helpers do not activate portal SQL.
- `/opt/restcontrol-saas/data`: original durable encryption key only; PostgreSQL holds the registry,
  directory 0700/files 0600, owned by unprivileged `restcontrol-saas`.
- `/opt/restcontrol-saas/backups`: private consistent PostgreSQL JSON+manifest+key snapshots.
- `/opt/restcontrol-saas/runtime.env`: root-owned 0600 systemd EnvironmentFile,
  holding CHAIKA_SAAS_DATABASE_URL, CHAIKA_SAAS_SUPABASE_URL,
  CHAIKA_SAAS_ANON_KEY and CHAIKA_SAAS_AUTH_ADMIN_KEY. Never put the operator
  restore DSN here.
- `/opt/supabase/volumes/proxy/caddy/restcontrol-run`: socket-only directory,
  owned by `restcontrol-saas`, 0700. The existing Caddy runs as root in its
  container and can access the socket through its existing `/etc/caddy` mount.
  No registry, key or backups are placed in that shared proxy directory.

One systemd application process listens on a Unix socket; there is no public
application TCP port. Uvicorn does not trust forwarded headers. Peer-based login
limits are shared behind this proxy; global limits remain active.

## Publish and verify

1. Record exact source commits and build the separate `saas-admin` frontend.
   Do not commit database files, keys, passwords or local runtime directories.
2. Apply the additive restcontrol migration in the existing Supabase PostgreSQL.
   Use its restricted restcontrol_backend role for the runtime DSN; preserve
   existing dashboard grants and Auth users. Import the legacy SQLite snapshot
   once using app.saas_admin.import_registry and the existing owner Auth UUID.
   Keep the original private credentials.key. Legacy password hashes/sessions
   are not imported. Direct CLI bootstrap is disabled.
3. Provision the service user, private directories and pinned virtualenv. Install
   the supplied service and backup units. Point `current` to the exact release.
4. Start only `restcontrol-saas`. Check `/api/saas-admin/health` through the socket
   with `Host: rc.chaika.team`; require `mode: production`. Verify owner/tenant
   login, password-change gate, Origin/CSRF failures and Secure cookie attributes.
5. Add only the `rc.caddy` import to the existing proxy. Validate before a graceful
   reload using the same environment as Caddy's startup (its auth password is
   converted to bcrypt at startup). Preserve all existing sites and containers.
6. Point the rc DNS record to this server, validate HTTPS and browser login.
   Company routing is checked against the exact active registry domain/API host.
   For tenant release, follow the same-origin edge section below. Keep the shared
   React App on App `254029`, route the exact tenant domain to this VPS, and proxy
   static bytes to the App technical host. Verify DNS/TLS, own-slug isolation,
   CSRF, Strict cookie login/logout and the required business views in a browser.

7. Run a PostgreSQL backup with the runtime DSN: all registry tables are read
   in one repeatable-read snapshot; copied BFF sessions are always omitted.
   Restore only with CHAIKA_SAAS_RESTORE_DATABASE_URL in a separate operator
   environment, into an isolated already migrated empty database/schema and
   a new private key directory. Preserved Auth UUIDs must exist there. Compare
   row counts/content and key digest. Never restore into populated production.
   Enable the daily timer and keep an additional private copy off-host.

Backups contain encrypted credentials **and the matching key**; restrict access
to the whole directory. This is disaster recovery, not protection from a host
administrator. Snapshot retention is manual in this first small registry;
monitor disk usage before enabling large client datasets here.

## Rollback

Stop only `restcontrol-saas`, preserve its latest data snapshot, repoint `current`
to the previous compatible release and start it. Never restore old business
data just to roll back code. For a first-release routing rollback, restore only
the saved rc DNS/proxy change; leave dashboard, Supabase and the document worker
as they were. Schema-incompatible restoration requires a separate reviewed
recovery plan and invalidates all restored sessions.

Restore never includes Auth passwords or rewrites Auth users. Snapshot hashes
verify consistency, not authenticity against an attacker with backup write access.
The backup service has network access for PostgreSQL and write access only to the
backup directory; it neither requires host pg_dump nor Docker/root privileges.

## Same-origin tenant edge (2026-10-10)

The earlier split-origin slice above is historical. New tenant and optional
payment domains use [tenant-site.caddy](tenant-site.caddy): `/api` and `/api/*`
reach the existing gateway socket with the original Host; all other GET/HEAD
requests fetch the shared Timeweb App `254029` through its fixed technical HTTPS
host. Apps remains the source of frontend builds and automatic deployments.
The main Chaika dashboard, rc site and existing API alias blocks are unchanged.

1. Save the reviewed domain in the company registry (or activate the verified
   payment-domain request); create its single A record to the current gateway
   VPS. Remove conflicting AAAA/CNAME records only as part of that domain change.
   No new `api.<domain>` DNS record is required.
2. Import `tenant-site.caddy` once into the existing Caddyfile. Add one explicit
   HTTPS site block per approved domain, containing `import restcontrol_tenant_site`.
   Do not use wildcard hosts or on-demand TLS. The existing private socket path
   and `RESTCONTROL_EDGE_TOKEN` must match the running gateway; the service manager
   supplies the capability, never a committed plaintext value.
3. Validate the complete candidate configuration with the running Caddy version
   and its startup environment, then gracefully reload. Preserve the previous
   configuration and DNS records for rollback; do not replace unrelated sites.
4. Verify HTTPS `/api/saas-context` resolves the exact company and `/` loads the
   current App build. Check login/session/CSRF, another tenant slug rejection and
   payment-domain staff-route rejection. Test an existing guest link by reading
   it only; do not create payments for routing verification. Browser acceptance
   is distinct from a successful HTTP status.
5. Verify `/deposit/<id>?token=...`, `/p/<code>` and SSO callback navigation send
   only `/index.html` without a query to Timeweb; assets keep their hashed paths
   but lose queries. Cookie, Authorization, Referer and gateway headers are stripped
   upstream, and upstream Set-Cookie is removed. Non-API methods other than GET/HEAD
   return 405. The static allowlist supports the current flat Vite outputs and
   named public assets; update it deliberately if the build introduces new formats.

Keep old API alias routing for cached frontends and existing provider callbacks.
New callbacks use the frontend origin after child/payment code is updated.
Rollback restores only the affected exact site blocks and their DNS, retaining
compatible backend code and business data. Do not enable request logs containing
capability paths, query strings or authentication headers.
