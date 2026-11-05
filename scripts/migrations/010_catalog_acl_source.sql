ALTER TABLE catalog_acl_ownership
  ADD COLUMN source text NOT NULL DEFAULT 'departments';

ALTER TABLE catalog_acl_ownership
  DROP CONSTRAINT catalog_acl_ownership_pkey,
  ADD PRIMARY KEY (catalog_url, reg, source, control_name);
