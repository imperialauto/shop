"""Owner texts resolve to the right repair order; texted app problems reach the Fix list once."""
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import owner_focus
from app.database import Base, get_db
from app.imperial_task_model import PROBLEM_CATEGORY, ImperialTask
from app.main import app
from app.models import Customer, LineItem, RepairOrder, Vehicle
from app.routes import internal_messaging

KEY = "test-internal-secret"
HEADERS = {"X-Internal-Key": KEY}
HEADLIGHT = ("Note I'm not able to upload photos to our number I believe it's eight for Antoine Chevy Impala SS. "
             "I just installed a headlight bulb and it re-the headlight is currently working")


class OwnerFocusTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        for patcher in (patch.object(internal_messaging, "INTERNAL_API_KEY", KEY),
                        patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        with self.Session() as db:
            def job(ro_id, first, last, year, make, model, lines=(), status="in_progress"):
                customer = Customer(first_name=first, last_name=last, phone=f"60255501{ro_id:02d}")
                vehicle = Vehicle(customer=customer, year=year, make=make, model=model)
                ro = RepairOrder(id=ro_id, customer=customer, vehicle=vehicle, status=status, concern="x")
                db.add_all([customer, vehicle, ro])
                for description in lines:
                    db.add(LineItem(repair_order=ro, description=description, quantity=1, unit_price=10))
            job(8, "Antwon", "Test", 2009, "Chevrolet", "Impala", ("Headlight bulb", "Lower radiator hose", "Oil leak diagnosis"))
            job(12, "Dana", "Jeep", 2014, "Jeep", "Compass")
            job(14, "Robert", "Focus", 2016, "Ford", "Focus", status="delivered")
            db.commit()

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def resolve(self, text):
        with self.Session() as db:
            return owner_focus.resolve(db, text)

    def test_number_word_misspelt_name_and_vehicle_find_ro_8(self):
        found = self.resolve(HEADLIGHT)
        self.assertEqual(found["ro"]["roId"], 8)
        self.assertEqual(found["ro"]["label"], "RO #8")
        self.assertEqual([line["description"] for line in found["ro"]["lines"]],
                         ["Headlight bulb", "Lower radiator hose", "Oil leak diagnosis"])

    def test_explicit_ro_number_wins_and_unknown_number_is_reported(self):
        self.assertEqual(self.resolve("RO 12 is ready")["ro"]["roId"], 12)
        self.assertEqual(self.resolve("job #8 headlight done")["ro"]["roId"], 8)
        missing = self.resolve("RO 999 status?")
        self.assertEqual((missing["missing"], missing["missingType"]), (999, "ro"))

    def test_approval_numbers_are_not_repair_orders(self):
        self.assertEqual(self.resolve("111 more context?"), {})
        self.assertEqual(self.resolve("YES 12"), {})  # a bare number alone is not enough to pick a job

    def test_two_close_matches_ask_instead_of_guessing(self):
        with self.Session() as db:
            customer = Customer(first_name="Antwon", last_name="Other", phone="6025550199")
            vehicle = Vehicle(customer=customer, year=2010, make="Chevrolet", model="Impala")
            db.add_all([customer, vehicle, RepairOrder(id=20, customer=customer, vehicle=vehicle, status="intake")])
            db.commit()
        found = self.resolve("Antoine Chevy Impala headlight")
        self.assertNotIn("ro", found)
        self.assertEqual(sorted(c["roId"] for c in found["candidates"]), [8, 20])

    def test_delivered_jobs_are_not_matched_by_name(self):
        self.assertEqual(self.resolve("Robert Ford Focus clutch"), {})

    def test_internal_lookup_requires_key(self):
        self.assertEqual(self.client.get("/internal/messaging/owner-focus", params={"text": HEADLIGHT}).status_code, 401)
        found = self.client.get("/internal/messaging/owner-focus", params={"text": HEADLIGHT}, headers=HEADERS).json()
        self.assertEqual(found["ro"]["roId"], 8)

    def test_texted_problem_is_saved_once_and_returns_its_real_id(self):
        body = {"messageId": "AC359ed48", "text": HEADLIGHT, "context": "RO #8 Antwon Test 2009 Chevrolet Impala"}
        first = self.client.post("/internal/imperial/tasks/problems", json=body, headers=HEADERS)
        again = self.client.post("/internal/imperial/tasks/problems", json=body, headers=HEADERS)  # Quo redelivery
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertEqual(first.json()["status"], "open")
        with self.Session() as db:
            rows = db.query(ImperialTask).filter(ImperialTask.category == PROBLEM_CATEGORY).all()
            self.assertEqual(len(rows), 1)
            self.assertIn("Page: RO #8 Antwon Test", rows[0].details)
        listed = self.client.get("/internal/imperial/tasks/problems", headers=HEADERS).json()["problems"]
        self.assertEqual([p["id"] for p in listed], [first.json()["id"]])

    def test_texted_problem_rejected_without_key_or_when_tasks_are_off(self):
        body = {"messageId": "m", "text": HEADLIGHT}
        self.assertEqual(self.client.post("/internal/imperial/tasks/problems", json=body).status_code, 401)
        with patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "false"}):
            self.assertNotEqual(self.client.post("/internal/imperial/tasks/problems", json=body, headers=HEADERS).status_code, 200)


class TypedRecordIdTests(OwnerFocusTests):
    """QA on #191: "approval 12" or "task 12" must never resolve toward RO #12."""

    def resolve(self, text):
        with self.Session() as db:
            return owner_focus.resolve(db, text)

    def test_typed_numbers_are_not_repair_orders(self):
        for text in ("approval 12", "approval #12 is done", "task 12", "task twelve", "fix list 12",
                     "invoice 12 paid?", "queue item 12", "what's the status on approval 12"):
            with self.subTest(text=text):
                out = self.resolve(text)
                self.assertNotIn("ro", out)
                self.assertNotIn("candidates", out)
                self.assertNotEqual(out.get("missing"), 12)
                self.assertTrue(out["refs"])
                self.assertNotEqual(out["refs"][0]["type"], "ro")

    def test_ro_words_still_resolve(self):
        for text in ("RO 12", "ro #12", "repair order 12", "job number 12", "R.O. 12", "work order 12"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text)["ro"]["roId"], 12)

    def test_typed_number_with_a_name_still_finds_that_customers_job(self):
        out = self.resolve("task 3 for Antwon's Impala")
        self.assertEqual(out["ro"]["roId"], 8)
        self.assertEqual(out["refs"], [{"type": "task", "id": 3}])

    def test_typed_number_cannot_tip_a_close_match_to_the_wrong_ro(self):
        # Dana's job is RO #12 (a Compass). The model alone scores 2; before this fix the typed
        # number added 2 more and tipped it over the line to RO #12.
        out = self.resolve("approval 12 Compass")
        self.assertNotIn("ro", out)

    def test_named_records_are_looked_up_with_type_and_state(self):
        with self.Session() as db:
            task = ImperialTask(source="dashboard", external_id="t1", title="Order tank", category="general")
            problem = ImperialTask(source="owner_sms", external_id="p1", title="Can't upload", category=PROBLEM_CATEGORY)
            db.add_all([task, problem])
            db.commit()
            task_id, problem_id = task.id, problem.id
        out = self.resolve(f"task {task_id} and fix list {problem_id} and approval 999")
        records = {(r["type"], r["id"]): r for r in out["records"]}
        self.assertTrue(records[("task", task_id)]["exists"])
        self.assertEqual(records[("task", task_id)]["label"], f"task #{task_id}")
        self.assertEqual(records[("fix", problem_id)]["label"], f"Fix list #{problem_id}")
        self.assertFalse(records[("approval", 999)]["exists"])

    def test_missing_ro_says_ro(self):
        out = self.resolve("RO 404")
        self.assertEqual(out["missing"], 404)
        self.assertEqual(out["missingType"], "ro")

if __name__ == "__main__":
    unittest.main()
