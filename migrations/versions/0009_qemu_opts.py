"""Per-node QEMU options, and a data volume that outlives a wipe."""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_qemu_opts"
down_revision: str | None = "0008_mac_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS qemu_opts JSONB NOT NULL "
        "DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE nodes DROP COLUMN IF EXISTS qemu_opts")
