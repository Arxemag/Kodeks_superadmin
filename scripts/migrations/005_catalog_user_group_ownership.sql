CREATE TABLE IF NOT EXISTS catalog_user_group_ownership (
  reg VARCHAR(50) NOT NULL,
  user_uid TEXT NOT NULL,
  catalog_user_id TEXT,
  group_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (reg, user_uid),
  CHECK (jsonb_typeof(group_ids) = 'array')
);
