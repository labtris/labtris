"""Add the `ovs` network kind.

An Open vSwitch segment, for labs that want OpenFlow, OVSDB or per-port
QoS. Nothing about existing rows changes — this only widens the enum so a
network can declare itself OVS-backed.

Revision ID: 0021_ovs_network_kind
Revises: 0020_ssh_keys
"""

from __future__ import annotations

from alembic import op

revision: str = "0021_ovs_network_kind"
down_revision: str | None = "0020_ssh_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block on older servers, and
    # alembic wraps migrations in one. autocommit_block is the supported way.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE network_kind ADD VALUE IF NOT EXISTS 'ovs'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum. Rewriting the type means
    # rewriting every dependent column, which is not worth it to undo a
    # widening that breaks nothing.
    pass
