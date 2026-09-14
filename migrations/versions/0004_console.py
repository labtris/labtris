"""Per-node remote-console settings (guacd VNC/RDP target + credentials)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_console"
down_revision: str | None = "0003_multihost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS console JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE nodes DROP COLUMN IF EXISTS console")
