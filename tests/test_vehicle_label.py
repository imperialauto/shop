import unittest
from app.models import Vehicle


class VehicleLabelTests(unittest.TestCase):
    def test_full(self):
        self.assertEqual(Vehicle(year=2016, make="Ford", model="Fusion").label, "2016 Ford Fusion")

    def test_missing_year_is_not_none(self):
        self.assertEqual(Vehicle(year=None, make="Ford", model="Focus").label, "Ford Focus")

    def test_unknown_placeholders(self):
        self.assertEqual(Vehicle(make="Unknown", model="Unknown").label, "Vehicle details not given yet")


if __name__ == "__main__":
    unittest.main()
