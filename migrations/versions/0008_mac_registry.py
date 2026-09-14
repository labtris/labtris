"""Reserve MAC addresses instead of hoping random ones do not collide."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_mac_registry"
down_revision: str | None = "0007_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mac_registry (
          mac        MACADDR PRIMARY KEY,
          owner_id   CHAR(26) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS mac_registry_owner_idx ON mac_registry (owner_id)")
    # Adopt what is already in use, so existing labs cannot have an address
    # handed out from under them by the first allocation after this migration.
    op.execute(
        """
        INSERT INTO mac_registry (mac, owner_id)
        SELECT mac, id FROM interfaces
        ON CONFLICT (mac) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mac_registry")
