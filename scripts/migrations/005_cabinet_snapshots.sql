-- Снимки исходной конфигурации кабинетов (инфобордов) до первого уточнения по опроснику.
-- Пишется один раз на (reg, board_id): эталон, от которого считается каждое следующее уточнение.
-- Позволяет не только удалять виджеты невыбранных вариантов, но и ВОЗВРАЩАТЬ ранее удалённые.

CREATE TABLE IF NOT EXISTS cabinet_snapshots (
  id BIGSERIAL PRIMARY KEY,
  reg VARCHAR(50) NOT NULL,
  board_id VARCHAR(100) NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  config JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS cabinet_snapshots_reg_board_idx
  ON cabinet_snapshots (reg, board_id);
