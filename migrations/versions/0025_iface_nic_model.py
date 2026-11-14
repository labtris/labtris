"""Per-interface NIC model on QEMU nodes (NULL: the node's model)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0025_iface_nic_model"
down_revision: str | None = "0024_assistant_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE interfaces ADD COLUMN IF NOT EXISTS nic_model TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE interfaces DROP COLUMN IF EXISTS nic_model")
