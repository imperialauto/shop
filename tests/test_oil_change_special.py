"""Full synthetic oil change special. Made-up data, private database, no live services."""
import json
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import business_settings, estimate_prefill, oil_reminders, public_site
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
            db.refresh(ro)
            # The oil change due reminder counts the special, so these customers get the reminder too.
            self.assertTrue(oil_reminders.is_oil_change(ro))
        # Oil and filter are taxed parts; the rest of the price is untaxed labor.
        by_type = {(li.item_type, li.taxable): li for li in lines}
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(li.approval is None for li in lines))  # not sent to the customer
        oil = next(li for li in lines if "per quart" in li.description)
        self.assertEqual((oil.quantity, oil.unit_price, oil.taxable), (5, 10.99, True))
        filt = next(li for li in lines if "filter" in li.description.lower() and li.item_type == "part" and li is not oil)
        self.assertEqual((filt.unit_price, filt.taxable), (12.99, True))
        labor = by_type[("labor", False)]
        self.assertAlmostEqual(labor.unit_price, 12.05)
        self.assertIn("$10.99 each", labor.description)
        self.assertAlmostEqual(sum(li.quantity * li.unit_price for li in lines), 79.99)
        # Tax on parts only: $67.94 x 9.2% = $6.25, so $86.24 in all.
        from app.routes.invoices import SHOP_TAX_RATE
        tax = round((oil.quantity * oil.unit_price + filt.unit_price) * SHOP_TAX_RATE, 2)
        self.assertEqual((tax, round(79.99 + tax, 2)), (6.25, 86.24))

    def test_a_price_below_the_oil_and_filter_stays_one_taxed_line(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        with Session(engine) as db:
            db.add_all([Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100"),
                        Vehicle(id=1, customer_id=1, make="Ford")])
            ro = RepairOrder(customer_id=1, vehicle_id=1, status="open")
            db.add(ro)
            db.flush()
            env, store = catalog({"oil-change": {"price": 49.99}})
            with env, store:
                estimate_prefill.oil_change_line(db, ro, public_site.bookable_service("oil-change"))
            db.commit()
            lines = db.query(LineItem).filter_by(repair_order_id=ro.id).all()
        self.assertEqual([(li.item_type, li.unit_price, li.taxable) for li in lines], [("part", 49.99, True)])

    def test_booking_page_names_the_extra_quart_price(self):
        env, store = catalog({"oil-change": {"price": 79.99}})
        with env, store:
            self.assertIn("$10.99 each", public_site.bookable_service("oil-change").description)

if __name__ == "__main__":
    unittest.main()
