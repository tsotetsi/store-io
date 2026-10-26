"""Pure helpers for order intake.

Keeping reference resolution and intent comparison free of I/O makes them cheap
to unit test and makes the idempotency rules explicit in one place.
"""

import hashlib
import uuid
from typing import Any

from order.app.schemas import OrderCreateSchema


def resolve_order_ref(payload: OrderCreateSchema, idempotency_key: str | None) -> str:
    """Pick the order reference: client-owned, derived from the key, or random.

    Deriving it from ``X-Idempotency-Key`` means the database primary key keeps
    protecting the order even if the Redis cache is flushed between retries.
    """
    if payload.order_ref:
        return payload.order_ref

    if idempotency_key:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]
        return f"web-{digest}"

    return f"web-{uuid.uuid4().hex[:12]}"


def intent_signature(
    customer_id: str,
    items: list[dict[str, Any]],
) -> tuple[str, tuple[tuple[str, int], ...]]:
    """Canonical form of what a client asked for, used to detect key reuse.

    Repeated SKUs are merged, so ``[{BAN, 2}]`` and ``[{BAN, 1}, {BAN, 1}]`` are
    the same intent and replay instead of conflicting.
    """
    totals: dict[str, int] = {}

    for item in items:
        sku = str(item["sku"])
        totals[sku] = totals.get(sku, 0) + int(item["qty"])

    return customer_id, tuple(sorted(totals.items()))
