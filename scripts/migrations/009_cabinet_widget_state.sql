CREATE TABLE IF NOT EXISTS catalog_cabinet_widget_state (
  catalog_url text NOT NULL,
  board_id text NOT NULL,
  reg text NOT NULL,
  archive jsonb NOT NULL DEFAULT '[]'::jsonb,
  pending jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (catalog_url, board_id),
  CHECK (jsonb_typeof(archive) = 'array'),
  CHECK (pending IS NULL OR jsonb_typeof(pending) = 'object')
);
