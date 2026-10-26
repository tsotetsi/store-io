"""Unit tests for the pure intake helpers."""

import re

import pytest

from order.app.intake import intent_signature, resolve_order_ref
from order.app.schemas import OrderCreateSchema


def make_payload(
    *,
    order_ref: str | None = None,
    customer_id: str = "cust-1",
    items: tuple[tuple[str, int], ...] = (("BAN-001", 2),),
) -> OrderCreateSchema:
    return OrderCreateSchema(
        order_ref=order_ref,
        customer_id=customer_id,
        items=[{"sku": sku, "qty": qty} for sku, qty in items],
    )


class TestResolveOrderRef:
    def test_client_reference_wins_over_idempotency_key(self) -> None:
        payload = make_payload(order_ref="web-100045")

        assert resolve_order_ref(payload, "key-from-client") == "web-100045"

    def test_idempotency_key_derives_a_stable_reference(self) -> None:
        payload = make_payload()

        first = resolve_order_ref(payload, "checkout-abc")
        second = resolve_order_ref(payload, "checkout-abc")

        assert first == second
        assert re.fullmatch(r"web-[0-9a-f]{12}", first)

    def test_different_keys_derive_different_references(self) -> None:
        payload = make_payload()

        assert resolve_order_ref(payload, "key-a") != resolve_order_ref(payload, "key-b")

    def test_without_key_or_reference_the_reference_is_random(self) -> None:
        payload = make_payload()

        first = resolve_order_ref(payload, None)
        second = resolve_order_ref(payload, None)

        assert first != second
        assert first.startswith("web-")


class TestIntentSignature:
    def test_split_lines_collapse_to_the_same_intent(self) -> None:
        one_line = make_payload(items=(("BAN-001", 2),))
        two_lines = make_payload(items=(("BAN-001", 1), ("BAN-001", 1)))

        assert intent_signature(
            one_line.customer_id, [item.model_dump() for item in one_line.items]
        ) == intent_signature(
            two_lines.customer_id, [item.model_dump() for item in two_lines.items]
        )

    def test_line_order_does_not_change_the_intent(self) -> None:
        first = make_payload(items=(("BAN-001", 1), ("APP-002", 3)))
        second = make_payload(items=(("APP-002", 3), ("BAN-001", 1)))

        assert intent_signature(
            first.customer_id, [item.model_dump() for item in first.items]
        ) == intent_signature(second.customer_id, [item.model_dump() for item in second.items])

    @pytest.mark.parametrize(
        ("customer_id", "items"),
        [
            ("cust-2", (("BAN-001", 2),)),  # different customer
            ("cust-1", (("BAN-001", 3),)),  # different quantity
            ("cust-1", (("APP-002", 2),)),  # different sku
        ],
    )
    def test_a_different_order_is_a_different_intent(
        self,
        customer_id: str,
        items: tuple[tuple[str, int], ...],
    ) -> None:
        original = make_payload()
        changed = make_payload(customer_id=customer_id, items=items)

        assert intent_signature(
            original.customer_id, [item.model_dump() for item in original.items]
        ) != intent_signature(changed.customer_id, [item.model_dump() for item in changed.items])
