"""Real users, and labs that belong to one."""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_users"
down_revision: str | None = "0009_qemu_opts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id            CHAR(26) PRIMARY KEY,
          username      TEXT NOT NULL UNIQUE,
          display_name  TEXT NOT NULL DEFAULT '',
          password_hash TEXT NOT NULL,
          role          TEXT NOT NULL DEFAULT 'user',
          disabled      BOOLEAN NOT NULL DEFAULT false,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_login_at TIMESTAMPTZ
        )
        """
    )
    # Nullable: labs that predate users have no owner, and inventing one would
    # be a lie. The UI shows them as unowned until somebody claims them.
    op.execute("ALTER TABLE labs ADD COLUMN IF NOT EXISTS owner_id CHAR(26)")
    op.execute(
        "ALTER TABLE labs ADD CONSTRAINT labs_owner_fk "
        "FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS labs_owner_idx ON labs (owner_id)")


def downgrade() -> None:
    op.execute("ALTER TABLE labs DROP CONSTRAINT IF EXISTS labs_owner_fk")
    op.execute("ALTER TABLE labs DROP COLUMN IF EXISTS owner_id")
    op.execute("DROP TABLE IF EXISTS users")
