"""EVE-NG parity: style, suspend, configs, configsets, templates, tasks."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_eve_parity"
down_revision: str | None = "0001_phase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE = """
ALTER TABLE nodes ADD COLUMN style JSONB NOT NULL DEFAULT '{}';
ALTER TABLE nodes ADD COLUMN paused BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE nodes ADD COLUMN startup_config TEXT;
ALTER TABLE labs ADD COLUMN configsets JSONB NOT NULL DEFAULT '{}';
CREATE TABLE templates (
  id          CHAR(26) PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  runtime     runtime_kind NOT NULL DEFAULT 'docker',
  image       TEXT NOT NULL,
  cmd         JSONB,
  env         JSONB NOT NULL DEFAULT '{}',
  icon        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE tasks (
  id          CHAR(26) PRIMARY KEY,
  lab_id      CHAR(26) REFERENCES labs(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  progress    INTEGER NOT NULL DEFAULT 0,
  total       INTEGER NOT NULL DEFAULT 0,
  message     TEXT NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON tasks (lab_id);
"""

_DOWNGRADE = """
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS templates;
ALTER TABLE labs DROP COLUMN IF EXISTS configsets;
ALTER TABLE nodes DROP COLUMN IF EXISTS startup_config;
ALTER TABLE nodes DROP COLUMN IF EXISTS paused;
ALTER TABLE nodes DROP COLUMN IF EXISTS style;
"""


def _run(sql: str) -> None:
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def upgrade() -> None:
    _run(_UPGRADE)


def downgrade() -> None:
    _run(_DOWNGRADE)
