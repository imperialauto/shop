"""Full synthetic oil change special. Made-up data, private database, no live services."""
import json
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import business_settings, estimate_prefill, public_site
from app.database import Base
from app.models import Customer, LineItem, RepairOrder, Vehicle


def catalog(entries):
    return patch.dict(os.environ, {"PUBLIC_SERVICE_CATALOG": json.dumps(entries)}), patch.object(business_settings, "_load", return_value={})


class OilChangeSpecialTests(unittest.TestCase):
    def test_hidden_until_the_owner_saves_a_price(self):
        env, store = catalog({"diagnostic": {"price": 75}})
        with env, store:
            slugs = [s.slug for s in public_site.approved_bookable_services()]
            self.assertNotIn("oil-change", slugs)
            self.assertNotIn("oil-change", [s.slug for s in public_site.requestable_services()])
            self.assertIsNone(public_site.bookable_service("oil-change"))

    def test_bookable_at_the_saved_price(self):
        env, store = catalog({"oil-change": {"price": 79.99, "minutes": 30}})
        with env, store:
            special = public_site.bookable_service("oil-change")
            self.assertEqual(special.price_label, "$79.99")
            self.assertEqual(special.minutes, 30)
            self.assertIn("5 quarts", special.description)

    def test_listed_on_the_settings_page_for_its_price(self):
        from app.routes import business_settings_page
        env, store = catalog({})
        with env, store:
            slugs = [s["slug"] for s in business_settings_page.current()["services"]]
        self.assertIn("oil-change", slugs)

    def test_booking_starts_the_job_with_one_unsent_package_line(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        with Session(engine) as db:
            db.add_all([Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100"),
                        Vehicle(id=1, customer_id=1, make="Ford")])
            ro = RepairOrder(customer_id=1, vehicle_id=1, status="open")
            db.add(ro)
            db.flush()
            env, store = catalog({"oil-change": {"price": 79.99}, "diagnostic": {"price": 75}})
            with env, store:
                self.assertIsNotNone(estimate_prefill.oil_change_line(db, ro, public_site.bookable_service("oil-change")))
                self.assertIsNone(estimate_prefill.oil_change_line(db, ro, public_site.bookable_service("diagnostic")))
            db.commit()
            lines = db.query(LineItem).filter_by(repair_order_id=ro.id).all()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].unit_price, 79.99)
        self.assertIsNone(lines[0].approval)  # not sent to the customer
        self.assertIn("up to 5 qt", lines[0].description)


if __name__ == "__main__":
    unittest.main()
