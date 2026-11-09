"""Owner-approved first online booking price examples. QA only; no production prices changed."""
import json
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401 -- strictly synthetic database
from app import business_settings, public_site


QA_ONLY_CATALOG = {
    "diagnostic": {"price": 75, "price_type": "exact", "minutes": 60, "mobile": False},
    "brake-inspection": {"price": 49, "price_type": "exact", "minutes": 60, "mobile": False},
}


class OwnerApprovedQAPricingTests(unittest.TestCase):
    def test_approved_first_two_services_without_enabling_public_booking(self):
        def settings(key):
            return json.dumps(QA_ONLY_CATALOG) if key == "booking_services" else None

        with patch.object(business_settings, "raw", side_effect=settings), patch.dict(os.environ, {
            "PUBLIC_BOOKING_SERVICES": "diagnostic,brake-inspection",
            "PUBLIC_FORMS_ENABLED": "",
        }):
            services = {service.slug: service for service in public_site.bookable_services()}
            self.assertEqual(set(services), {"diagnostic", "brake-inspection"})
            self.assertEqual(services["diagnostic"].price, 75)
            self.assertEqual(services["brake-inspection"].price, 49)
            for service in services.values():
                self.assertEqual(service.price_type, "exact")
                self.assertFalse(service.mobile)
            self.assertFalse(public_site.forms_enabled(read_only=False))
            self.assertFalse(public_site.forms_enabled(read_only=True))

    def test_no_unapproved_service_is_firmly_priced(self):
        def settings(key):
            return json.dumps(QA_ONLY_CATALOG) if key == "booking_services" else None

        with patch.object(business_settings, "raw", side_effect=settings), patch.dict(os.environ, {
            "PUBLIC_BOOKING_SERVICES": "diagnostic,brake-inspection,oil-service,ac-check",
            "PUBLIC_FORMS_ENABLED": "",
        }):
            self.assertEqual(
                [service.slug for service in public_site.bookable_services()],
                ["diagnostic", "brake-inspection"],
            )


if __name__ == "__main__":
    unittest.main()
