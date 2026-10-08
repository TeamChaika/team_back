# Dedicated RestControl deployment

This deployment runs only `app.saas_admin`, not `app.portal`, the scheduler or the
documents worker. The SPA and API use `https://rc.chaika.team` and relative API
URLs. There is no cross-origin API and no CORS wildcard. Host and Origin checks,
CSRF and Secure host-only cookies are enforced by the application.

## Layout

- `/opt/restcontrol-saas/releases/<backend-sha>-<frontend-sha>/`: immutable source
  (`backend/app/saas_admin` plus empty `app/__init__.py`) and built `frontend/`.
- `/opt/restcontrol-saas/current`: symlink to the deployed release.
- `/opt/restcontrol-saas/venv`: runtime from `requirements-saas-admin.txt`.
- `/opt/restcontrol-saas/data`: durable registry and matching encryption key,
  directory 0700/files 0600, owned by unprivileged `restcontrol-saas`.
- `/opt/restcontrol-saas/backups`: private validated database+key snapshots.
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
2. Export the latest local registry with `backup --clear-sessions`, transfer it
   over SSH and use `restore` into a new empty `data` directory. Preserve company
   identifiers, password hashes, encrypted connections and audit history. Never
   overwrite an existing live data directory with an older local snapshot.
3. Provision the service user, private directories and pinned virtualenv. Install
   the supplied service and backup units. Point `current` to the exact release.
4. Start only `restcontrol-saas`. Check `/api/saas-admin/health` through the socket
   with `Host: rc.chaika.team`; require `mode: production`. Verify owner/tenant
   login, password-change gate, Origin/CSRF failures and Secure cookie attributes.
5. Add only the `rc.caddy` import to the existing proxy. Validate before a graceful
   reload using the same environment as Caddy's startup (its auth password is
   converted to bcrypt at startup). Preserve all existing sites and containers.
6. Point only the rc DNS record to this server, validate HTTPS and browser login.
   Client domains remain a separate deployment task; do not expose Chaika data
   or reuse owner cookies there.
7. Run a backup, restore to an isolated directory, validate it, restart the SaaS
   service and compare non-secret row counts/content digests. Enable the daily
   backup timer. Keep an additional private copy off-host after deployment.

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
