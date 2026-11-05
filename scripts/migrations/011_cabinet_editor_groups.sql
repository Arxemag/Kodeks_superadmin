CREATE TABLE IF NOT EXISTS catalog_cabinet_editor_groups (
  catalog_url text NOT NULL,
  branch_id text NOT NULL,
  reg text NOT NULL,
  root_id text NOT NULL,
  group_id bigint,
  boards text[] NOT NULL DEFAULT '{}',
  tracked_uids text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (catalog_url, branch_id),
  UNIQUE (catalog_url, group_id)
);
