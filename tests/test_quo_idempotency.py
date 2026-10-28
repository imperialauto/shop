"""Regression coverage for duplicate Quo message delivery.

Run against an isolated test database, never production.
"""
from unittest.mock import MagicMock, patch
from app.routes.internal_messaging import log_communication
from app.models import Communication, Customer


def test_existing_external_message_is_not_inserted():
    db = MagicMock()
    customer = Customer(id=12, first_name="Test", last_name="Customer", phone="6025550100")
    existing = Communication(id=45, customer_id=12, repair_order_id=None, external_id="quo-123")
    db.query.return_value.filter.return_value.all.return_value = [customer]
    db.query.return_value.filter.return_value.first.return_value = existing
    with patch("app.routes.internal_messaging._find_customer_by_phone", return_value=customer):
        result = log_communication(
            {"phone": "+16025550100", "direction": "inbound",
             "body": "Test message", "externalId": "quo-123"},
            db=db, _auth=True,
        )
    assert result["duplicate"] is True
    db.add.assert_not_called()
    db.commit.assert_not_called()
