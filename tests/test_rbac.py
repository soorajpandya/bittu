from app.services.rbac_service import RBACService


def test_alias_normalization_plural_singular() -> None:
    svc = RBACService()
    aliases = svc._aliases("order.create")
    assert "order.create" in aliases
    assert "orders.create" in aliases


def test_wildcard_match() -> None:
    svc = RBACService()
    assert svc._has_wildcard("orders.update", {"orders.*"}) is True
    assert svc._has_wildcard("payments.refund", {"payments.create"}) is False
