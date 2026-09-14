"""Multi-host control plane: Host registry + vxlan network kind."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_multihost"
down_revision: str | None = "0002_eve_parity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE = """
ALTER TYPE network_kind ADD VALUE IF NOT EXISTS 'vxlan';
CREATE TABLE hosts (
  id          CHAR(26) PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  endpoint    TEXT NOT NULL,
  token       TEXT,
  underlay_ip TEXT,
  is_local    BOOLEAN NOT NULL DEFAULT false,
  reachable   BOOLEAN NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE networks ADD COLUMN vni INTEGER;
ALTER TABLE ifname_registry DROP CONSTRAINT ifname_registry_kind_check;
ALTER TABLE ifname_registry ADD CONSTRAINT ifname_registry_kind_check
  CHECK (kind IN ('tap','bridge','veth','vxlan'));
CREATE TABLE network_hosts (
  network_id CHAR(26) NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
  host_id    CHAR(26) NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
  vxlan_ifname TEXT,
  PRIMARY KEY (network_id, host_id)
);
"""

_DOWNGRADE = """
DROP TABLE IF EXISTS network_hosts;
ALTER TABLE networks DROP COLUMN IF EXISTS vni;
DROP TABLE IF EXISTS hosts;
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
