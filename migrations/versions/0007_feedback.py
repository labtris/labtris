"""In-app annotations: what the user says is broken, and where."""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_feedback"
down_revision: str | None = "0006_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
          id         CHAR(26) PRIMARY KEY,
          note       TEXT NOT NULL,
          status     TEXT NOT NULL DEFAULT 'open',
          context    JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS feedback_status_idx ON feedback (status, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback")
