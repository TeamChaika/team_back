-- Additive Telegram delivery ledger; document records/history remain untouched.
BEGIN;
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;
CREATE TABLE IF NOT EXISTS native_telegram_cleanup (
    kind text NOT NULL CHECK (kind IN ('waybill','writeoff')),
    document_id bigint NOT NULL,
    approved_version integer NOT NULL,
    PRIMARY KEY (kind,document_id)
);
CREATE TABLE IF NOT EXISTS native_telegram_messages (
    chat_id bigint NOT NULL,
    message_id bigint NOT NULL,
    kind text NOT NULL CHECK (kind IN ('waybill','writeoff')),
    document_id bigint NOT NULL,
    version integer NOT NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','pending','deleted','buttons_removed','unavailable')),
    attempts integer NOT NULL DEFAULT 0,
    remove_buttons boolean NOT NULL DEFAULT false,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id,message_id)
);
CREATE INDEX IF NOT EXISTS native_telegram_messages_due ON native_telegram_messages(next_attempt_at) WHERE state='pending';
CREATE INDEX IF NOT EXISTS native_telegram_messages_document ON native_telegram_messages(kind,document_id,version);
-- Reserve cleanup for documents already approved when deploying. No guessed legacy chat IDs.
INSERT INTO native_telegram_cleanup(kind,document_id,approved_version)
SELECT 'waybill',id,version FROM waybills WHERE status='Sent' OR submission_state IN ('queued','sending','sent','unknown')
UNION ALL SELECT 'writeoff',id,version FROM writeoffs WHERE status='Sent' OR submission_state IN ('queued','sending','sent','unknown')
ON CONFLICT(kind,document_id) DO NOTHING;
-- Queued approval remains final for Telegram even if iiko failed afterwards.
INSERT INTO native_telegram_cleanup(kind,document_id,approved_version)
SELECT kind,document_id,max(version) FROM native_dispatch GROUP BY kind,document_id
ON CONFLICT(kind,document_id) DO UPDATE SET
    approved_version=greatest(native_telegram_cleanup.approved_version,excluded.approved_version);
-- Saved callbacks provide exact original chat/message IDs for some historic previews.
WITH callbacks AS (
    SELECT regexp_match(data #>> '{callback_query,data}',
        '^(confirmWaybill|denyWaybill|confirmReceipt|rejectReceipt|confirmwriteoff|denywriteoff):([0-9]{1,18}):([0-9]{1,9})$') AS parts,
        CASE WHEN data #>> '{callback_query,message,chat,id}' ~ '^-?[0-9]{1,18}$'
            THEN (data #>> '{callback_query,message,chat,id}')::bigint END AS chat_id,
        CASE WHEN data #>> '{callback_query,message,message_id}' ~ '^[0-9]{1,18}$'
            THEN (data #>> '{callback_query,message,message_id}')::bigint END AS message_id
    FROM native_bot_updates
    WHERE data #> '{callback_query,message,reply_markup,inline_keyboard}' @>
        jsonb_build_array(jsonb_build_array(jsonb_build_object(
            'callback_data',data #>> '{callback_query,data}')))
), parsed AS (
    SELECT CASE WHEN parts[1] IN ('confirmwriteoff','denywriteoff') THEN 'writeoff'
        ELSE 'waybill' END AS kind, parts[2]::bigint AS document_id,
        parts[3]::integer AS version, chat_id,message_id FROM callbacks
    WHERE parts IS NOT NULL AND chat_id IS NOT NULL AND message_id IS NOT NULL
)
INSERT INTO native_telegram_messages(chat_id,message_id,kind,document_id,version,state)
SELECT p.chat_id,p.message_id,p.kind,p.document_id,p.version,
    CASE WHEN c.approved_version>=p.version THEN 'pending' ELSE 'active' END
FROM parsed p
JOIN (SELECT 'waybill' AS kind,id AS document_id,version FROM waybills
    UNION ALL SELECT 'writeoff',id,version FROM writeoffs) d
    ON d.kind=p.kind AND d.document_id=p.document_id AND d.version>=p.version
LEFT JOIN native_telegram_cleanup c ON c.kind=p.kind AND c.document_id=p.document_id
WHERE p.chat_id<>0 AND p.message_id>0 AND p.document_id>0 AND p.version>0
ON CONFLICT(chat_id,message_id) DO NOTHING;
-- Rerunning a backfill also schedules ledger rows previously imported as active.
UPDATE native_telegram_messages m SET state='pending',next_attempt_at=now(),updated_at=now()
FROM native_telegram_cleanup c WHERE m.kind=c.kind AND m.document_id=c.document_id
    AND m.version<=c.approved_version AND m.state='active';
GRANT SELECT,INSERT,UPDATE ON native_telegram_cleanup,native_telegram_messages TO chaika_iiko_app;
COMMIT;
