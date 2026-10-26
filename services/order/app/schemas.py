from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OrderItemSchema(BaseModel):
    """A single line item of an order as submitted by a client."""

    sku: str = Field(..., min_length=1, max_length=64, examples=["BAN-001"])
    qty: int = Field(..., gt=0, le=1000, examples=[2])


class PricedOrderItemSchema(OrderItemSchema):
    """A line item with the price that was locked in when the order was placed."""

    unit_price_cents: int = Field(..., ge=0, examples=[199])
    line_total_cents: int = Field(..., ge=0, examples=[398])


class OrderCreateSchema(BaseModel):
    """Payload accepted by ``POST /orders``."""

    order_ref: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
        examples=["web-100045"],
        description="Client-owned idempotency key. Re-submitting the same value never creates a second order.",
    )
    customer_id: str = Field(..., min_length=1, max_length=64, examples=["cust-42"])
    items: list[OrderItemSchema] = Field(..., min_length=1, max_length=50)


class OrderResponseSchema(BaseModel):
    """Order representation returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    order_ref: str
    customer_id: str
    items: list[PricedOrderItemSchema]
    total_cents: int
    status: str
    pricing_source: str
    created_at: datetime
    # Duplicate submissions are signalled by the X-Idempotent-Replay response
    # header rather than a body field, so a cached replay is byte-identical to
    # the original response.
