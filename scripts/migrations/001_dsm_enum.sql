DO $$ BEGIN
  CREATE TYPE mapping_status_enum AS ENUM ('active', 'archived');
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;
