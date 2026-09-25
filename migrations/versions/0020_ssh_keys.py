"""SSH pubkey registry (Phase K1b).

Each row is one authorized OpenSSH-format pubkey (e.g. `ssh-ed25519
AAAAC3Nz... me@laptop`) owned by exactly one Labtris user. The SSH proxy
(labtris_api.ssh_proxy) checks incoming connections against this table
so tools can sign in without passing a JWT as password.

Fingerprint is stored separately so the SSH-proxy auth callback can do an
O(indexed-lookup) match instead of loading + parsing every registered key
on every attempt. Format: `SHA256:<base64>` (matches `ssh-keygen -l`
output).

`last_used_at` is a courtesy — lets `labtris ssh-keys list` show whether
a key is still active before you remove it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_ssh_keys"
down_revision: str | None = "0019_link_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ssh_keys",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column(
            "user_id",
            sa.CHAR(26),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("algorithm", sa.Text, nullable=False),
        sa.Column("key_body", sa.Text, nullable=False),
        # SHA256:<44-char base64> — 51 chars total. Unique across all users
        # so a stolen pubkey cannot silently masquerade as a second user.
        sa.Column("fingerprint", sa.Text, nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ssh_keys")
