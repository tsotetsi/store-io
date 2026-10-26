"""Add outbox events and processed order ledger

Revision ID: c4d5e6f7a8b9
Revises: 4b7de7ba2125
Create Date: 2026-10-25 20:06:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "4b7de7ba2125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add at-least-once safety: a dedupe ledger and a feedback outbox."""
    op.create_table(
        "processed_orders",
        sa.Column("order_ref", sa.String(length=64), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("order_ref"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_key", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_outbox_events_routing_key"), "outbox_events", ["routing_key"])
    op.create_index(op.f("ix_outbox_events_created_at"), "outbox_events", ["created_at"])
    op.create_index(op.f("ix_outbox_events_published_at"), "outbox_events", ["published_at"])


def downgrade() -> None:
    """Drop the outbox and ledger tables."""
    op.drop_index(op.f("ix_outbox_events_published_at"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_created_at"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_routing_key"), table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("processed_orders")
