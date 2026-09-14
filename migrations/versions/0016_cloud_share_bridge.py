"""Let more than one cloud network reuse the same host bridge.

Cloud networks come in two flavours now. A cloud on a bare NIC has to be
unique — the kernel allows only one master per interface, so a second cloud
would steal the NIC and leave the first bridge with no uplink. A cloud that
reuses an OS-owned bridge (netplan br0, EVE-NG's pnet0) has no such
restriction: nothing is enslaved, lab veths just join the bridge as extra
ports. Two icons on the canvas can safely name the same host bridge, and
that's the natural way to draw "these two lab links share one office-LAN
uplink" — mirroring EVE-NG, where multiple lab links can drop onto the same
pnet0.

The uniqueness gate for the enslave case is enforced in the API path
(routers/networks.py), where it can consult netd about what the picked name
actually is. The DB-level `UNIQUE` on `networks.host_ifname` was a belt on
top of that, from before reuse existed, and it now refuses the very case
this migration is here to allow.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_cloud_share_bridge"
down_revision: str | None = "0015_template_spec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("networks_host_ifname_key", "networks", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("networks_host_ifname_key", "networks", ["host_ifname"])
