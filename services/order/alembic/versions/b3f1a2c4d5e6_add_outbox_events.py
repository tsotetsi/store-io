"""Add outbox events

Revision ID: b3f1a2c4d5e6
Revises: 7f4c2b91a0de
Create Date: 2026-10-25 20:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1a2c4d5e6"
down_revision: str | Sequence[str] | None = "7f4c2b91a0de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store events in the same transaction as the order that produced them."""
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_key", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_outbox_events_routing_key"), "outbox_events", ["routing_key"])
    op.create_index(op.f("ix_outbox_events_created_at"), "outbox_events", ["created_at"])
    op.create_index(op.f("ix_outbox_events_published_at"), "outbox_events", ["published_at"])


def downgrade() -> None:
    """Drop the outbox table."""
    op.drop_index(op.f("ix_outbox_events_published_at"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_created_at"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_routing_key"), table_name="outbox_events")
    op.drop_table("outbox_events")
