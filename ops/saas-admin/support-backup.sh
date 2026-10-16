#!/bin/sh
# Fixed local support-schema backup. No application env or credentials are loaded.
set -eu
umask 077

[ "$(/usr/bin/id -u)" -eq 0 ]
directory=/var/lib/restcontrol-support-backups
[ -d "$directory" ]
[ ! -L "$directory" ]
[ "$(/usr/bin/stat -c '%u:%g:%a' "$directory")" = "0:0:700" ]

stamp=$(/usr/bin/date -u +%Y%m%dT%H%M%SZ)
temporary=$(/usr/bin/mktemp "$directory/support-$stamp-XXXXXX.partial")
# Failure leaves a private .partial file for inspection; it is never a completed backup.
/usr/bin/timeout --signal=TERM --kill-after=10s 280s \
    /usr/bin/docker exec supabase-db \
    pg_dump -U supabase_admin -d postgres --format=custom --schema=support \
    --lock-wait-timeout=10s > "$temporary" 2>/dev/null
[ -s "$temporary" ]
/usr/bin/mv -- "$temporary" "${temporary%.partial}.dump"
