# Dedicated RestControl deployment

Updated 2026-10-08. The dedicated VPS `5.42.103.76` runs `app.saas_admin`.
The owner SPA/API remain same-origin at `https://rc.chaika.team`, with the existing
Supabase PostgreSQL `restcontrol` registry and Supabase Auth.

The first tenant slice uses the original React dashboard on shared Timeweb static
App `254029`, without Chaika branding. On publication, `iiko.tdpay.ru` returns to
that App; `api.iiko.tdpay.ru` serves its SaaS API on this VPS. Credentialed CORS
accepts only the exact active registry frontend origin and its own tenant slug;
host-only Secure SameSite=Strict cookies and CSRF are required. No wildcard or
Chaika fallback is allowed. `VITE_PRIMARY_ORIGINS` retains the exact primary
dashboard and technical hosts.

Overview/Sales read only the company's own Chain, with a 300-second process cache.
Other business modules are not ready. This slice and split-origin deployment await
publication and real browser verification; earlier tenant login evidence does not
prove this release. See [the contract](../../docs/tenant-dashboard.md).

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
   For the tenant release, build the normal shared React App for App `254029`,
   route only `api.iiko.tdpay.ru` to this VPS and restore `iiko.tdpay.ru` to the App.
   Verify DNS/TLS, exact credentialed CORS (including error responses/preflight),
   own-slug isolation, CSRF, Strict cookie login/logout and real Overview/Sales.
   These steps are pending for the new slice.
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
