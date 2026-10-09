-- Stable bot IDs isolate Telegram cursors and message IDs across replacements.
-- Unknown historic rows remain unassigned and are never sent to a newly configured bot.
SET LOCAL search_path={documents},pg_catalog;
ALTER TABLE native_bot_updates ADD COLUMN bot_id text NOT NULL DEFAULT '';
ALTER TABLE native_bot_updates DROP CONSTRAINT native_bot_updates_pkey;
ALTER TABLE native_bot_updates ADD PRIMARY KEY(bot_id,id);
ALTER TABLE native_telegram_messages ADD COLUMN bot_id text NOT NULL DEFAULT '';
ALTER TABLE native_telegram_messages DROP CONSTRAINT native_telegram_messages_pkey;
ALTER TABLE native_telegram_messages ADD PRIMARY KEY(bot_id,chat_id,message_id);
