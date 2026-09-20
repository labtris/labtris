"""Lab-scoped ready hooks: three columns on the labs table.

The runner (labtris_api/runtime/hooks.py) fires user-defined checks —
ping, http, command, serial_wait — once a lab reaches a "ready" state.
The definition is YAML, editable in the UI and via `labtris lab hooks
apply`; the DB caches both the raw source and the parsed form.

Three columns, all nullable so this migration is safe to run against an
existing instance without a data backfill:

- `hooks_source` TEXT — canonical YAML the user writes; kept verbatim
  so comments and ordering round-trip. Absent when the lab has never
  had a hooks block.
- `hooks` JSONB — parsed form (`ready_when`, `hooks[]` array). Server
  reads this; the runner walks it.
- `hooks_state` JSONB — per-run outcome list. Wiped on each re-run;
  written by the runner as each hook advances phase pending -> running
  -> passed | failed.

No server_default: NULL is meaningful ("no hooks defined on this lab"),
and defaulting to an empty object would make the runner think every lab
has hooks and iterate an empty list on every state transition.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017_lab_hooks"
down_revision: str | None = "0016_cloud_share_bridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("labs", sa.Column("hooks_source", sa.Text(), nullable=True))
    op.add_column("labs", sa.Column("hooks", JSONB(), nullable=True))
    op.add_column("labs", sa.Column("hooks_state", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("labs", "hooks_state")
    op.drop_column("labs", "hooks")
    op.drop_column("labs", "hooks_source")
