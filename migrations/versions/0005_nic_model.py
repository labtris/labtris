"""Per-node QEMU NIC model (virtio-net-pci, e1000, ...)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_nic_model"
down_revision: str | None = "0004_console"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS nic_model TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE nodes DROP COLUMN IF EXISTS nic_model")
