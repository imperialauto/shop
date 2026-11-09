"""Names for unnamed customers from their own Quo texts and calls. Made-up data, no live services."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import name_finder
from app.database import Base, get_db
from app.main import app
from app.models import Customer
from app.routes import dispatch, internal_messaging

KEY = "fixture-internal-key"


def found(phone):
    return {"name": {"first": "Dana", "last": "Lee", "quote": "this is Dana Lee", "source": "text"}}, None


class NameFinderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        name_finder._last_sweep = 0.0
        with self.Session() as db:
            db.add_all([Customer(id=1, first_name="Unknown", last_name="(0177)", phone="+16235550177"),
                        Customer(id=2, first_name="Pat", last_name="Sample", phone="+16025550100", name_source="quo_contact"),
                        Customer(id=3, first_name="Unknown", last_name="(0188)", phone="+16235550188"),
                        Customer(id=4, first_name="Unknown", last_name="(0199)", phone=None)])
            db.commit()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def test_found_name_is_saved_with_proof_and_source(self):
        with self.Session() as db:
            c = db.get(Customer, 1)
            self.assertEqual(name_finder.check(db, c, found), "found")
            db.commit()
            c = db.get(Customer, 1)
            self.assertEqual((c.first_name, c.last_name, c.name_source), ("Dana", "Lee", "text"))
            self.assertIn('"this is Dana Lee"', c.notes)
            self.assertIsNotNone(c.name_checked_at)

    def test_saved_names_are_never_touched(self):
        calls = []
        with self.Session() as db:
            self.assertEqual(name_finder.check(db, db.get(Customer, 2), lambda p: calls.append(p) or found(p)), "skipped")
        self.assertEqual(calls, [])

    def test_no_name_marks_checked_and_outage_does_not(self):
        with self.Session() as db:
            c = db.get(Customer, 1)
            self.assertEqual(name_finder.check(db, c, lambda p: ({"name": None}, None)), "none")
            self.assertIsNotNone(c.name_checked_at)
            other = db.get(Customer, 3)
            self.assertEqual(name_finder.check(db, other, lambda p: (None, "unavailable")), "unavailable")
            self.assertIsNone(other.name_checked_at)
            self.assertEqual(name_finder.check(db, other, lambda p: ({"name": None, "error": "ai_unavailable"}, None)), "unavailable")

    def test_sweep_checks_a_few_stops_on_outage_and_waits_between_runs(self):
        result = name_finder.sweep(self.Session, found)
        self.assertEqual(result["found"], 2)  # the two unnamed customers with phones
        self.assertEqual(name_finder.sweep(self.Session, found), {"skipped": "recent"})
        with self.Session() as db:
            self.assertEqual(name_finder.progress(db), {"unnamed": 1, "waiting": 0})  # the one with no phone stays
        name_finder._last_sweep = 0.0
        with self.Session() as db:
            for c in db.query(Customer).filter(Customer.id.in_((1, 3))).all():
                c.first_name, c.name_checked_at = "Unknown", None
            db.commit()
        down = name_finder.sweep(self.Session, lambda p: (None, "unavailable"))
        self.assertEqual(down["unavailable"], 1)  # stopped after the first failure

    def test_a_new_text_from_an_unnamed_customer_rechecks_them(self):
        with self.Session() as db:
            db.get(Customer, 1).name_checked_at = datetime(2026, 11, 1, tzinfo=timezone.utc)
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        with patch.object(internal_messaging, "INTERNAL_API_KEY", KEY), patch.object(dispatch, "OWNER_PHONE", "+16025550199"):
            TestClient(app).post("/internal/dispatch/intake", headers={"X-Internal-Key": KEY},
                                 json={"conversationId": "CN1", "messageId": "m1", "phone": "+16235550177", "body": "This is Dana by the way"})
        with self.Session() as db:
            self.assertIsNone(db.get(Customer, 1).name_checked_at)
            self.assertEqual(db.query(Customer).count(), 4)  # no duplicate made


if __name__ == "__main__":
    unittest.main()
