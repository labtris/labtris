"""Remember which configset a lab is currently running.

Applying a set is the interesting event in a teaching lab — "everyone back to
`broken-ospf`" — and until now nothing recorded that it happened. Without it
the UI can list four sets and say nothing about which one is in effect, which
is the one thing the person looking at the list wants to know.

The column is only honest if it is cleared the moment a node's config is
edited directly, so it is deliberately a claim about the last *apply*, not a
running comparison.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_active_configset"
down_revision: str | None = "0011_lab_folders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL rather than '': "no set has been applied" is genuinely unknown,
    # which is not the same as a set whose name happens to be empty.
    op.execute("ALTER TABLE labs ADD COLUMN IF NOT EXISTS active_configset TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE labs DROP COLUMN IF EXISTS active_configset")
