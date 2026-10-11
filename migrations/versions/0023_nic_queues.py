"""Per-node virtio-net queue count (multi-queue + vhost opt-in)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_nic_queues"
down_revision: str | None = "0022_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS nic_queues INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE nodes DROP COLUMN IF EXISTS nic_queues")
