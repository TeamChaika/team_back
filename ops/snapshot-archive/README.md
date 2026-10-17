# Private archive broker

Install the service after adjusting executable/worktree paths to the actual host.
No TCP listener or public route is configured. The broker authenticates accepted
Unix sockets with Linux SO_PEERCRED; caller headers never select a company.

Root-managed `/etc/restcontrol/snapshot-archive.env` (0600) contains the direct
`CHAIKA_SNAPSHOT_ARCHIVE_MODE=s3`, endpoint, bucket, region, access and secret keys.
Do not put this file in a tenant directory or copy these variables into tenants.
Create `/etc/restcontrol/snapshot-archive-grants` root-owned 0755, with root-owned,
non-group/world-writable ancestors. Only the privileged runtime operator writes
UID.json grant files; the broker derives company schema from the UUID and checks
the live dedicated account on every request. Removing a grant revokes future
requests. Reprovisioning renews a grant; remove obsolete grants when retiring accounts.

Add to the trusted fleet operator_template:

    "snapshot_archive": {
      "mode": "broker",
      "socket": "/run/restcontrol-archive/archive.sock",
      "grants_directory": "/etc/restcontrol/snapshot-archive-grants"
    }

The fleet refresh propagates this configuration to existing operators. Their
prepare_manifest validates the isolated Linux identity and atomically creates
its grant. Child manifests receive only RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_MODE
and RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_BROKER_SOCKET. Scope is derived by the
shared archive adapter from the validated tenant runtime, never a caller field.

Apply the tenant migration before enabling the broker writer. Before evicting any
inline payload, test the live tenant PUT/GET and inventory restore path. Existing
external rows require this broker even if writes are paused; do not disable the
configuration or roll back to code without archive-reader compatibility.

Protocol: PUT/GET /v1/snapshots/{canonical UUID}/{SHA256 of object}. Bodies are
opaque versioned envelopes. PUT requires one Content-Length and verifies a full
GET before success. Fixed key: restcontrol/snapshots/v1/{derived analytics schema}/
{UUID}/{SHA256}.json.gz. Neither endpoint accepts bucket, scope, key or URL.
The broker exposes no delete/list API. Operator backfill uses the same key format.

The service uses two concurrent requests, 60-second socket timeout, and the shared
128 MiB object bound. S3 exceptions and payloads are not logged; HTTP errors are
sanitized. Provider timeouts come from the shared S3 adapter. Keep capacity checks
and service health monitoring outside tenant request handling.

Existing companies upgrade through the approved additive migration refresh:
the fleet detects the changed migration fingerprint, quiesces its owned company
processes, applies only the migration stage, and restores the prior readiness
state and independent acceptance evidence. The history plan version is unchanged;
this release does not request another 60-day synchronization. Inspect the updated
migration fingerprint and restarted processes before enabling external-only writes.
A grant can be prepared before migration, but writer activation requires both the
schema upgrade and live broker availability.
