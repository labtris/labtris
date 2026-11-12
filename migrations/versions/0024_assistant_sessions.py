"""Assistant conversations kept on the server.

Until now a conversation lived in the browser's localStorage and the server
started every message from nothing: closing the tab or signing out ended the
turn, and another device saw an empty chat. These two tables hold the
conversation so a turn keeps running without a browser attached, and any
device can read it back and continue.

Both cascade from their lab: deleting a lab deletes what was said about it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_assistant_sessions"
down_revision: str | None = "0023_nic_queues"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_sessions",
        sa.Column("id", postgresql.CHAR(26), primary_key=True),
        sa.Column("user_id", postgresql.CHAR(26),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("lab_id", postgresql.CHAR(26),
                  sa.ForeignKey("labs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False, server_default="assistant"),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_assistant_sessions_user_id", "assistant_sessions", ["user_id"])
    op.create_index("ix_assistant_sessions_lab_id", "assistant_sessions", ["lab_id"])
    op.create_table(
        "assistant_events",
        sa.Column("session_id", postgresql.CHAR(26),
                  sa.ForeignKey("assistant_sessions.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_table("assistant_events")
    op.drop_table("assistant_sessions")
