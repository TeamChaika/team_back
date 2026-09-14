-- A request for 2023-05-30 returned a shift opened 2023-05-29T23:25:14.297.
-- Coverage describes the requested day; open_date retains the unmodified source value.
-- Keep all returned rows visible and deduplicate UUIDs across request snapshots.
-- Rollback: deploy a forward migration restoring the previous view's
-- WHERE o.open_date::date=d.open_day. No stored observations are removed either way.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
CREATE OR REPLACE VIEW chaika.cash_shifts WITH (security_invoker=true) AS
SELECT DISTINCT ON (d.source_id,o.id) o.*,d.source_id,
       d.observed_at AS last_seen_at,d.last_snapshot_id
FROM chaika.cash_shift_days d
JOIN chaika.cash_shift_observations o ON o.snapshot_id=d.last_snapshot_id
ORDER BY d.source_id,o.id,d.observed_at DESC;
