"""Add order totals, pricing source and timestamps

Revision ID: 7f4c2b91a0de
Revises: 1d344c43c94b
Create Date: 2026-10-25 18:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f4c2b91a0de"
down_revision: str | Sequence[str] | None = "1d344c43c94b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the columns that make an order self-describing.

    Existing rows default to a zero total: their prices were never captured, and
    inventing one by re-pricing would misrepresent history.
    """
    op.add_column(
        "orders",
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "orders",
        sa.Column("pricing_source", sa.String(length=16), nullable=False, server_default="live"),
    )
    op.add_column(
        "orders",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # The defaults only exist to backfill old rows; new writes always supply a
    # value, so drop them from the total/pricing columns.
    op.alter_column("orders", "total_cents", server_default=None)
    op.alter_column("orders", "pricing_source", server_default=None)

    op.create_index(op.f("ix_orders_status"), "orders", ["status"], unique=False)


def downgrade() -> None:
    """Remove the pricing snapshot columns."""
    op.drop_index(op.f("ix_orders_status"), table_name="orders")
    op.drop_column("orders", "updated_at")
    op.drop_column("orders", "created_at")
    op.drop_column("orders", "pricing_source")
    op.drop_column("orders", "total_cents")
