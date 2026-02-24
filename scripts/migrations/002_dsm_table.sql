CREATE TABLE IF NOT EXISTS department_service_mapping (
  id BIGSERIAL PRIMARY KEY,
  department_id UUID NOT NULL,
  service_group_name VARCHAR(255) NOT NULL,
  reg VARCHAR(50) NOT NULL,
  client_id VARCHAR(100) NOT NULL,
  client_name VARCHAR(255) NOT NULL,
  mapping_status mapping_status_enum NOT NULL DEFAULT 'active'
);
