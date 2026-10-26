from pydantic import BaseModel, ConfigDict, Field


class InventoryResponseSchema(BaseModel):
    """Inventory representation returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    sku: str = Field(..., examples=["BAN-001"])
    name: str = Field(..., examples=["Bananas 1kg"])
    price_cents: int = Field(..., examples=[199])
    stock: int = Field(..., examples=[50])


class InventoryUpsertSchema(BaseModel):
    """Payload accepted by ``POST /inventory`` (used to seed products)."""

    sku: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    name: str = Field(..., min_length=1, max_length=128, examples=["Bananas 1kg"])
    price_cents: int = Field(..., ge=0, examples=[199])
    stock: int = Field(..., ge=0, examples=[50])
