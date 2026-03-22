"""
Unit tests for state machines.
"""
import pytest
from app.core.state_machines import (
    OrderStatus,
    ORDER_TRANSITIONS,
    validate_order_transition,
)
from app.core.exceptions import InvalidStateTransition


class TestOrderStateMachine:
    def test_pending_to_confirmed(self):
        validate_order_transition(OrderStatus.PENDING, OrderStatus.CONFIRMED)

    def test_pending_to_cancelled(self):
        validate_order_transition(OrderStatus.PENDING, OrderStatus.CANCELLED)

    def test_pending_to_delivered_invalid(self):
        with pytest.raises(InvalidStateTransition):
            validate_order_transition(OrderStatus.PENDING, OrderStatus.DELIVERED)

    def test_confirmed_to_preparing(self):
        validate_order_transition(OrderStatus.CONFIRMED, OrderStatus.PREPARING)

    def test_delivered_is_terminal(self):
        assert OrderStatus.DELIVERED.is_terminal

    def test_pending_is_not_terminal(self):
        assert not OrderStatus.PENDING.is_terminal

    def test_all_statuses_have_transitions(self):
        for status in OrderStatus:
            if not status.is_terminal:
                assert status in ORDER_TRANSITIONS, f"Missing transitions for {status}"
