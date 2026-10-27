-- Unified worker only. Intake commits after this journal is durable.
-- External catalog writes are not part of this PostgreSQL transaction.
CREATE TABLE IF NOT EXISTS integration_events (
    id bigserial PRIMARY KEY,
    source_namespace text NOT NULL,
    dedupe_key text NOT NULL,
    event_id text,
    fingerprint varchar(64) NOT NULL,
    source_topic text NOT NULL,
    source_partition integer NOT NULL,
    source_offset bigint NOT NULL,
    array_index integer NOT NULL DEFAULT -1,
    event_type text NOT NULL,
    entity_key text NOT NULL,
    reg text,
    source_revision bigint,
    revision_stream text,
    payload jsonb NOT NULL,
    disposition text,
    status text NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'running', 'retry_wait', 'waiting_dependency', 'succeeded', 'failed', 'conflict', 'superseded')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    received_at timestamptz NOT NULL DEFAULT now(),
    available_at timestamptz NOT NULL DEFAULT now(),
    deadline_at timestamptz NOT NULL,
    last_attempt_at timestamptz,
    completed_at timestamptz,
    error_code text,
    error_message text,
    result jsonb,
    conflicts_with bigint REFERENCES integration_events(id),
    retry_of bigint REFERENCES integration_events(id),
    retry_reason text,
    UNIQUE (source_namespace, dedupe_key)
);
CREATE INDEX IF NOT EXISTS ix_integration_events_claim
    ON integration_events (source_namespace, available_at, id)
    WHERE status IN ('received', 'retry_wait', 'waiting_dependency');
CREATE INDEX IF NOT EXISTS ix_integration_events_entity_order
    ON integration_events (source_namespace, entity_key, id)
    WHERE status IN ('received', 'running', 'retry_wait', 'waiting_dependency');
CREATE INDEX IF NOT EXISTS ix_integration_events_revision
    ON integration_events (source_namespace, reg, revision_stream, source_revision)
    WHERE status = 'succeeded' AND source_revision IS NOT NULL;
CREATE TABLE IF NOT EXISTS integration_outbox (
    id uuid PRIMARY KEY,
    operation_id bigint NOT NULL REFERENCES integration_events(id),
    sequence integer NOT NULL,
    topic text NOT NULL,
    message_key bytea,
    payload jsonb NOT NULL,
    headers jsonb NOT NULL DEFAULT '[]'::jsonb,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    available_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    error_message text,
    UNIQUE (operation_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_integration_outbox_pending
    ON integration_outbox (available_at, operation_id, sequence)
    WHERE sent_at IS NULL;
