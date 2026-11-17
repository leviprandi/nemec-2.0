"""Conversas fixadas: `conversations.is_pinned` (aprovada em 2026-11-16; sem tabela nova)."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE conversations ADD COLUMN is_pinned boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE conversations DROP COLUMN is_pinned")
