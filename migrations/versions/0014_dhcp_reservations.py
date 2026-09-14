"""Pin a port to an address on its NAT network.

A lab where r1 is 10.0.0.1 every time it boots is worth considerably more than
one where it is whatever the pool happened to hand out — instructions can name
addresses, a saved config keeps working, and a student comparing their output
to the notes sees the same numbers.

The MAC is already ours: interfaces get one from the registry at creation, and
dnsmasq keys reservations on exactly that. So this is one nullable column, not
a table: a reservation belongs to the port, and a port has at most one.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_dhcp_reservations"
down_revision: str | None = "0013_nat_and_vlan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE interfaces ADD COLUMN IF NOT EXISTS reserved_ip TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE interfaces DROP COLUMN IF EXISTS reserved_ip")
