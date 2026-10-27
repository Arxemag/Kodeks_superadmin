CREATE TABLE IF NOT EXISTS catalog_acl_ownership (
  catalog_url text NOT NULL,
  reg text NOT NULL,
  control_name text NOT NULL,
  group_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (catalog_url, reg, control_name),
  CHECK (jsonb_typeof(group_ids) = 'array')
);
