"""Folders for labs, as a path on the lab rather than a table of nodes.

A folder here is organisational, not structural: it exists to stop a list of
two hundred labs being a wall, and nothing about a lab's behaviour depends on
where it sits. A path string carries that with no join, no orphan rows when a
folder is emptied, and no second delete to get wrong. Moving a lab is one
UPDATE; renaming a folder is one UPDATE across a prefix.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_lab_folders"
down_revision: str | None = "0010_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty string rather than NULL for "no folder": it is the root, not an
    # unknown, and it keeps every ORDER BY and GROUP BY free of COALESCE.
    op.execute("ALTER TABLE labs ADD COLUMN IF NOT EXISTS folder TEXT NOT NULL DEFAULT ''")
    op.execute("CREATE INDEX IF NOT EXISTS ix_labs_folder ON labs (folder)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_labs_folder")
    op.execute("ALTER TABLE labs DROP COLUMN IF EXISTS folder")
