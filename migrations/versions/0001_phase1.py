"""Phase 1 schema — matches docs/05-phase1-spec.md §3 exactly."""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_phase1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE = """
CREATE TYPE node_state    AS ENUM ('defined','starting','running','stopping','stopped','failed');
CREATE TYPE network_kind  AS ENUM ('bridge','cloud');
CREATE TYPE runtime_kind  AS ENUM ('docker','qemu','containerlab','iol','dynamips');
CREATE TABLE labs (
  id          CHAR(26) PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  locked      BOOLEAN NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (name)
);
CREATE TABLE nodes (
  id            CHAR(26) PRIMARY KEY,
  lab_id        CHAR(26) NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  runtime       runtime_kind NOT NULL,
  image         TEXT NOT NULL,
  state         node_state NOT NULL DEFAULT 'defined',
  cpu_limit     NUMERIC(4,2),
  ram_mb        INTEGER,
  env           JSONB NOT NULL DEFAULT '{}',
  cmd           JSONB,
  runtime_ref   TEXT,
  last_error    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lab_id, name)
);
CREATE INDEX ON nodes (lab_id);
CREATE TABLE networks (
  id          CHAR(26) PRIMARY KEY,
  lab_id      CHAR(26) NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  kind        network_kind NOT NULL DEFAULT 'bridge',
  host_ifname TEXT UNIQUE,
  cloud_ref   TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lab_id, name),
  CHECK ((kind = 'cloud') = (cloud_ref IS NOT NULL))
);
CREATE TABLE interfaces (
  id          CHAR(26) PRIMARY KEY,
  node_id     CHAR(26) NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,
  name        TEXT NOT NULL,
  mac         MACADDR NOT NULL,
  host_ifname TEXT UNIQUE,
  network_id  CHAR(26) REFERENCES networks(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (node_id, idx),
  UNIQUE (node_id, name)
);
CREATE INDEX ON interfaces (network_id);
CREATE TABLE links (
  id           CHAR(26) PRIMARY KEY,
  lab_id       CHAR(26) NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
  a_iface_id   CHAR(26) NOT NULL REFERENCES interfaces(id) ON DELETE CASCADE,
  b_iface_id   CHAR(26) NOT NULL REFERENCES interfaces(id) ON DELETE CASCADE,
  network_id   CHAR(26) NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
  impair_ab    JSONB,
  impair_ba    JSONB,
  admin_up     BOOLEAN NOT NULL DEFAULT true,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (a_iface_id),
  UNIQUE (b_iface_id),
  CHECK (a_iface_id <> b_iface_id)
);
CREATE TABLE geometry (
  lab_id     CHAR(26) PRIMARY KEY REFERENCES labs(id) ON DELETE CASCADE,
  data       JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE ifname_registry (
  host_ifname TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('tap','bridge','veth')),
  owner_id    CHAR(26) NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWNGRADE = """
DROP TABLE IF EXISTS ifname_registry;
DROP TABLE IF EXISTS geometry;
DROP TABLE IF EXISTS links;
DROP TABLE IF EXISTS interfaces;
DROP TABLE IF EXISTS networks;
DROP TABLE IF EXISTS nodes;
DROP TABLE IF EXISTS labs;
DROP TYPE IF EXISTS runtime_kind;
DROP TYPE IF EXISTS network_kind;
DROP TYPE IF EXISTS node_state;
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
