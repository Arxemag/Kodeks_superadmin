DO $$ BEGIN
ALTER TABLE department_service_mapping
  ADD COLUMN IF NOT EXISTS catalog_group_id bigint,
  ADD COLUMN IF NOT EXISTS catalog_group_name text,
  ADD COLUMN IF NOT EXISTS catalog_group_origin text CHECK (catalog_group_origin IN ('created', 'adopted')),
  ADD COLUMN IF NOT EXISTS catalog_group_rename_allowed boolean NOT NULL DEFAULT false;

UPDATE department_service_mapping
SET catalog_group_name = service_group_name
WHERE catalog_group_name IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_dsm_reg_catalog_group
  ON department_service_mapping (reg, catalog_group_id)
  WHERE catalog_group_id IS NOT NULL;
END $$;
