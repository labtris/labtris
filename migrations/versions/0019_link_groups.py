"""LinkGroup schema — parallel Links between the same pair, for ECMP.

Adds a `link_groups` table (id, lab_id, name, hash_policy) and a
nullable `group_id` FK on `links`. The UNIQUE constraints on
`links.a_iface_id` and `links.b_iface_id` are dropped: they blocked
parallel wires between the same node pair, which is the whole point
of the group. Per-interface uniqueness enforcement moves to the API
layer (routers/links.create_link) — a DB-level unique that blocks
the LinkGroup use case for one edge case a router can catch is the
wrong tradeoff.

Rollback drops group_id and the table but does NOT re-add the UNIQUE
constraints — a lab that saved parallel Links between one pair would
fail the constraint recreate. Downgrade is best-effort; a full
rollback needs manual cleanup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_link_groups"
down_revision: str | None = "0018_node_opts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "link_groups",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column(
            "lab_id", sa.CHAR(26),
            sa.ForeignKey("labs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "hash_policy", sa.Text(),
            nullable=False, server_default="layer3+4",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "links",
        sa.Column(
            "group_id", sa.CHAR(26),
            sa.ForeignKey("link_groups.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    # Drop the two UNIQUE constraints. Names come from SQLAlchemy's
    # default: `<table>_<column>_key`.
    op.drop_constraint("links_a_iface_id_key", "links", type_="unique")
    op.drop_constraint("links_b_iface_id_key", "links", type_="unique")


def downgrade() -> None:
    op.drop_column("links", "group_id")
    op.drop_table("link_groups")
    # Deliberately don't re-add the UNIQUE constraints — see module
    # docstring.
