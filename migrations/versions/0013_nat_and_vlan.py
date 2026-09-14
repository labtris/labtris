"""NAT networks with DHCP, and VLAN-filtering (smart) bridges.

Three things a lab needs that a plain L2 segment cannot give it.

A NAT network is a bridge with a gateway address on the host and a masquerade
rule behind it, so guests reach the outside without being put on the host's own
LAN — which is what the `cloud` kind does, and why `cloud` is dangerous enough
to need an explicit confirmation. NAT is the answer for "these nodes need to
apt-get something" and it is by far the most common reason anyone reaches for
cloud today.

DHCP is part of the same object rather than a separate one: a NAT network whose
guests must be addressed by hand is most of the work of a NAT network with none
of the convenience, and `dhcp_first`/`dhcp_last` being NULL is how a network
says it hands out nothing.

VLAN filtering turns a bridge into an actual switch. Without it the kernel
forwards tagged frames unchanged and ignores the tags, so a trunk appears to
work by accident and an access port does not exist at all — you cannot teach
VLANs on it, because nothing is enforcing them. With it, ports carry a PVID and
a membership list, and 802.1ad gives the outer tag for QinQ.

The per-port columns live on interfaces rather than on a join table: a port
belongs to exactly one bridge at a time, so its VLAN membership has nowhere
else to be.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_nat_and_vlan"
down_revision: str | None = "0012_active_configset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block on older servers, and
    # alembic wraps migrations in one. autocommit_block is the supported way.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE network_kind ADD VALUE IF NOT EXISTS 'nat'")

    op.execute("ALTER TABLE networks ADD COLUMN IF NOT EXISTS subnet TEXT")
    op.execute("ALTER TABLE networks ADD COLUMN IF NOT EXISTS gateway TEXT")
    op.execute("ALTER TABLE networks ADD COLUMN IF NOT EXISTS dhcp_first TEXT")
    op.execute("ALTER TABLE networks ADD COLUMN IF NOT EXISTS dhcp_last TEXT")
    op.execute("ALTER TABLE networks ADD COLUMN IF NOT EXISTS dns_server TEXT")
    op.execute(
        "ALTER TABLE networks ADD COLUMN IF NOT EXISTS vlan_aware BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE networks ADD COLUMN IF NOT EXISTS vlan_proto TEXT NOT NULL DEFAULT '802.1Q'"
    )

    # A NAT network without a subnet has no gateway to offer and nothing to
    # masquerade, so the pair travels together or not at all.
    #
    # One statement per execute throughout: asyncpg prepares what it is given,
    # and a prepared statement cannot carry two commands. A DROP-then-ADD pair
    # in one string fails at runtime with a syntax error that says nothing
    # about which migration it came from.
    op.execute("ALTER TABLE networks DROP CONSTRAINT IF EXISTS nat_subnet_ck")
    op.execute(
        "ALTER TABLE networks ADD CONSTRAINT nat_subnet_ck "
        "CHECK (kind <> 'nat' OR (subnet IS NOT NULL AND gateway IS NOT NULL))"
    )
    op.execute("ALTER TABLE networks DROP CONSTRAINT IF EXISTS vlan_proto_ck")
    op.execute(
        "ALTER TABLE networks ADD CONSTRAINT vlan_proto_ck "
        "CHECK (vlan_proto IN ('802.1Q', '802.1ad'))"
    )

    op.execute("ALTER TABLE interfaces ADD COLUMN IF NOT EXISTS vlan_mode TEXT")
    op.execute("ALTER TABLE interfaces ADD COLUMN IF NOT EXISTS vlan_id INTEGER")
    # A trunk's allowed list, comma separated. A join table would be tidier and
    # would buy nothing: nothing ever queries "which ports carry VLAN 20"
    # without already having the bridge in hand.
    op.execute("ALTER TABLE interfaces ADD COLUMN IF NOT EXISTS trunk_vids TEXT")
    op.execute("ALTER TABLE interfaces DROP CONSTRAINT IF EXISTS vlan_mode_ck")
    op.execute(
        "ALTER TABLE interfaces ADD CONSTRAINT vlan_mode_ck "
        "CHECK (vlan_mode IS NULL OR vlan_mode IN ('access', 'trunk'))"
    )
    # 0 and 4095 are reserved. Rejecting them here means the kernel never has
    # to, and the kernel's refusal does not say which VLAN it disliked.
    op.execute("ALTER TABLE interfaces DROP CONSTRAINT IF EXISTS vlan_id_ck")
    op.execute(
        "ALTER TABLE interfaces ADD CONSTRAINT vlan_id_ck "
        "CHECK (vlan_id IS NULL OR (vlan_id BETWEEN 1 AND 4094))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE interfaces DROP CONSTRAINT IF EXISTS vlan_id_ck")
    op.execute("ALTER TABLE interfaces DROP CONSTRAINT IF EXISTS vlan_mode_ck")
    op.execute("ALTER TABLE interfaces DROP COLUMN IF EXISTS trunk_vids")
    op.execute("ALTER TABLE interfaces DROP COLUMN IF EXISTS vlan_id")
    op.execute("ALTER TABLE interfaces DROP COLUMN IF EXISTS vlan_mode")

    op.execute("ALTER TABLE networks DROP CONSTRAINT IF EXISTS vlan_proto_ck")
    op.execute("ALTER TABLE networks DROP CONSTRAINT IF EXISTS nat_subnet_ck")
    for column in ("vlan_proto", "vlan_aware", "dns_server", "dhcp_last", "dhcp_first",
                   "gateway", "subnet"):
        op.execute(f"ALTER TABLE networks DROP COLUMN IF EXISTS {column}")

    # The 'nat' enum value is deliberately left in place. Postgres cannot drop
    # one, and recreating the type would mean rewriting every row that
    # references it — a downgrade should not be the riskiest operation in the
    # file.
