"""Runtime-agnostic per-node options bag on `nodes`.

First consumer: the bmv2 P4 switch node kind (Phase E1) uses
`opts["p4_program"]` to pick a curated built-in P4 program at start.

Kept nullable rather than defaulting to `{}` — a NULL means "no
per-node opts at all" and lets the runtime skip the entire opts
read path. Also non-generalisable knobs (a container's hugepage
allocation, an RDMA node's rxe iface name, custom kernel modules
to load at boot) all belong here rather than each spawning its own
column and migration.

qemu_opts stays where it is (qemu-specific chipset/accel/boot-order
knobs) — that column is old and widely referenced. The new `opts`
column is the runtime-agnostic sibling.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018_node_opts"
down_revision: str | None = "0017_lab_hooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("opts", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "opts")
