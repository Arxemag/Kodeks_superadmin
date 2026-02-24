-- Таблица reg_services: reg_number -> base_url каталога
-- Используется Auth API, users worker, init_company worker

CREATE TABLE IF NOT EXISTS reg_services (
  reg_number text PRIMARY KEY,
  base_url text NOT NULL
);
