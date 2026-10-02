"""Audit log — every mutating operation, and who or what made it.

Motivated by the assistant. Once a model can exec into a node, rewrite a
config or delete a lab, "what happened to my lab" stops being a compliance
question and becomes a debugging one: the first thing anybody asks is whether
they did it or the AI did.

Reads are not recorded. Listing labs is not interesting, there are orders of
magnitude more of them, and a log nobody can scan is a log nobody reads.

`via` is the column this table exists for: "human" or "assistant", taken from
the signed token rather than a header, so a caller cannot relabel its own
writes.

Retention is finite by design. This grows without bound otherwise, and on a
host running thousands of nodes it grows fast — the 931-node start alone was
931 mutations. Pruned to LABTRIS_AUDIT_RETENTION_DAYS, default 7.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_audit_log"
down_revision: str | None = "0021_ovs_network_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.CHAR(26), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # The actor is denormalised on purpose: deleting a user must not erase
        # what that user did, and a FK with ON DELETE SET NULL would leave
        # rows that cannot say who they belonged to.
        sa.Column("actor_id", postgresql.CHAR(26), nullable=True),
        sa.Column("actor_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("via", sa.Text(), nullable=False, server_default="human"),
        sa.Column("method", sa.Text(), nullable=False),
        # The concrete path for reading, and the route template for grouping —
        # "/api/v1/nodes/{node_id}/start" answers "how often is this done"
        # where a thousand distinct paths do not.
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("lab_id", postgresql.CHAR(26), nullable=True),
        sa.Column("target_kind", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("detail", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    # Time is how this is always read — "what happened last week" — and the
    # index carries the pruning delete as well.
    op.create_index("ix_audit_at", "audit_log", [sa.text("at DESC")])
    # Per-lab history, which is the second question after "what happened".
    op.create_index("ix_audit_lab_at", "audit_log", ["lab_id", sa.text("at DESC")])
    # "What has the assistant been doing" deserves to be cheap, since it is
    # the reason the table exists.
    op.create_index("ix_audit_via_at", "audit_log", ["via", sa.text("at DESC")])


def downgrade() -> None:
    op.drop_index("ix_audit_via_at", table_name="audit_log")
    op.drop_index("ix_audit_lab_at", table_name="audit_log")
    op.drop_index("ix_audit_at", table_name="audit_log")
    op.drop_table("audit_log")
