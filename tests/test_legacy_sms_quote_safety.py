"""Legacy SMS intake cannot guess or automatically publish repair prices."""
import inspect
import unittest

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import EstimateSession, LineItem, RepairOrder
from app.routes import webhooks


class LegacySmsSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_ai_quote_generator_is_hard_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "Automatic SMS pricing is disabled"):
            await webhooks.generate_estimate({"year": 2013, "make": "Ford", "model": "Fusion"})

    def test_legacy_customer_path_contains_no_price_quote_or_free_inspection(self):
        source = inspect.getsource(webhooks.openphone_webhook)
        self.assertNotIn("Estimated total:", source)
        self.assertNotIn("No charge for the inspection", source)
        self.assertIn("No work starts without your OK", source)

    def test_legacy_intake_draft_has_no_billable_guess(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        try:
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                session = EstimateSession(phone_number="+16025550100", status="collecting")
                db.add(session)
                db.commit()
                ro, _ = webhooks.create_draft_ro(db, session, {
                    "name": "Sample Customer", "year": 2013, "make": "Ford",
                    "model": "Fusion", "complaint": "Test tank concern",
                    "engine": "2.5L", "mileage": "100000",
                }, {"needs_inspection": True, "notes": "Manual pricing review required."})
                self.assertEqual(ro.status, "intake")
                self.assertEqual(db.query(LineItem).filter(LineItem.repair_order_id == ro.id).count(), 0)
                self.assertEqual(db.query(RepairOrder).count(), 1)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
