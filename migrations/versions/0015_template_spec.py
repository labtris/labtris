"""Let a template carry how its machine is built, not just what image it runs.

A template used to be a name plus an image reference, which is the whole story
for Docker: the reference names immutable content, so two nodes from one
template are identical by construction.

QEMU is not like that. The image is a base that every node overlays, so a
template pointing at `ubuntu-24.04` reproduces a *pristine* Ubuntu — not the
one somebody spent an afternoon licensing and configuring. Saving that work
means flattening the overlay into a new image, and then remembering the things
the base catalog would otherwise have supplied: how much memory it needs, which
NIC its kernel can drive, whether its console is a framebuffer. Without those a
saved desktop appliance boots with CirrOS's 256 MB and no network.

One JSONB column rather than six typed ones, because the set differs per
runtime and will grow: a Docker template has nothing to put here, and the QEMU
keys are the catalog's own fields, which are still moving.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015_template_spec"
down_revision: str | None = "0014_dhcp_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "templates",
        sa.Column("spec", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    # Free text from whoever saved it — "licensed, 2026-09, do not delete" is
    # the kind of thing that stops a template being deleted by mistake a year
    # later, and there was nowhere to write it.
    op.add_column("templates", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("templates", "description")
    op.drop_column("templates", "spec")
