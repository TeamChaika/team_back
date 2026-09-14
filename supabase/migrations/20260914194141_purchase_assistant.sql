-- Private website conversations; no reporting data or iiko mutations.
CREATE TABLE chaika.assistant_conversations (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES chaika.web_users(id) ON DELETE CASCADE,
    access_hash text NOT NULL,
    context jsonb NOT NULL CHECK (jsonb_typeof(context) = 'object'),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, user_id)
);
CREATE TABLE chaika.assistant_turns (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 2000),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','failed')),
    answer text,
    sources jsonb NOT NULL DEFAULT '[]'::jsonb,
    usage jsonb NOT NULL DEFAULT '{}'::jsonb,
    model text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    FOREIGN KEY (conversation_id,user_id)
        REFERENCES chaika.assistant_conversations(id,user_id) ON DELETE CASCADE,
    CHECK (status <> 'completed' OR (answer IS NOT NULL AND length(answer) BETWEEN 1 AND 16000))
);
CREATE INDEX assistant_conversations_user_idx
    ON chaika.assistant_conversations(user_id,created_at DESC);
CREATE INDEX assistant_turns_user_idx ON chaika.assistant_turns(user_id,created_at DESC);
CREATE INDEX assistant_turns_conversation_idx ON chaika.assistant_turns(conversation_id,created_at);
ALTER TABLE chaika.assistant_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.assistant_conversations FORCE ROW LEVEL SECURITY;
ALTER TABLE chaika.assistant_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.assistant_turns FORCE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.assistant_conversations,chaika.assistant_turns FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON chaika.assistant_conversations TO chaika_backend;
GRANT SELECT,INSERT,UPDATE ON chaika.assistant_turns TO chaika_backend;
-- Only the authenticated server sets this transaction-local identity. It is never a
-- parameter accepted from the browser. The backend role has no BYPASSRLS privilege.
CREATE POLICY assistant_conversation_owner ON chaika.assistant_conversations TO chaika_backend
USING (user_id = nullif(current_setting('chaika.assistant_user',true),'')::uuid)
WITH CHECK (user_id = nullif(current_setting('chaika.assistant_user',true),'')::uuid);
CREATE POLICY assistant_turn_owner ON chaika.assistant_turns TO chaika_backend
USING (user_id = nullif(current_setting('chaika.assistant_user',true),'')::uuid)
WITH CHECK (user_id = nullif(current_setting('chaika.assistant_user',true),'')::uuid);
