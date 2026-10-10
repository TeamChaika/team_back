-- A standalone restaurant is one real primary source, never a synthetic RMS alias.
-- Keep the original cross-source constraint for replicated installations. The
-- standalone self-binding reuses the same actual department snapshot and carries
-- the verified source type; reference publication still validates group UUIDs.
ALTER TABLE {analytics}.rms_bindings DROP CONSTRAINT rms_bindings_check;
ALTER TABLE {analytics}.rms_bindings ADD CONSTRAINT rms_bindings_check CHECK (
    source_id <> chain_source_id OR (
        source_id = 'primary' AND chain_source_id = 'primary'
        AND details @> '{{"server_type":"STANDALONE_RMS","connection_id":"primary"}}'::jsonb
        AND chain_snapshot_id = rms_snapshot_id
    )
);
