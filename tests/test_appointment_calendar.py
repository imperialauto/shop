"""Confirmed appointments: owner reminder task, private phone-calendar feed, and the
assistant tools for naming customers and opening jobs. Made-up data only."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from test_agent_api import _Setup

from app import agent_keys, appointment_feed, appointment_followup
from app.agent_keys import AgentAction
from app.appointment_model import Appointment
from app.imperial_task_model import PERSONAL_CATEGORY, ImperialTask, ImperialTaskAudit
from app.models import Customer, LineItem, RepairOrder, Vehicle

SOON = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)


class _Data(_Setup):
    def setUp(self):
        super().setUp()
        with self.Session() as db:
            first = Customer(first_name="Testa", last_name="Example", phone="6025550199",
                             address="1 Fixture Way", email="fixture@example.invalid")
            unknown = Customer(first_name="Unknown", last_name="(0199)", phone="6025550198")
            db.add_all([first, unknown]); db.flush()
            v1 = Vehicle(customer_id=first.id, year=2004, make="Testmake", model="Fixture")
            v2 = Vehicle(customer_id=unknown.id)
            db.add_all([v1, v2]); db.flush()
            ro1 = RepairOrder(customer_id=first.id, vehicle_id=v1.id, concern="Call 602-555-0199 at 1 Fixture Way",
                              public_token="fixture-a")
            ro2 = RepairOrder(customer_id=unknown.id, vehicle_id=v2.id, public_token="fixture-b")
            db.add_all([ro1, ro2]); db.flush()
            self.ids = SimpleNamespace(customer=first.id, unknown=unknown.id, vehicle=v1.id, ro1=ro1.id, ro2=ro2.id)
            db.commit()

    def appointment(self, db, ro_id, status="confirmed", start=SOON, location="shop"):
        item = Appointment(repair_order_id=ro_id, resource_id="bay-1", starts_at=start,
                           ends_at=start + timedelta(hours=1), status=status, location=location,
                           travel_before_minutes=30 if location == "mobile" else 0)
        db.add(item); db.flush()
        return item


class ReminderTaskTests(_Data):
    def test_confirmed_appointment_makes_one_owner_task_due_at_start(self):
        with patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"}), self.Session() as db:
            item = self.appointment(db, self.ids.ro1)
            appointment_followup.add_reminder(db, item, "user:1")
            appointment_followup.add_reminder(db, item, "user:1")  # retry or second confirm
            db.commit()
            tasks = db.query(ImperialTask).filter_by(source="appointment").all()
            self.assertEqual(len(tasks), 1)
            task = tasks[0]
            self.assertEqual(task.category, PERSONAL_CATEGORY)
            self.assertEqual(task.assigned_user_id, 1)
            self.assertEqual(task.status, "open")
            due = task.due_at if task.due_at.tzinfo else task.due_at.replace(tzinfo=timezone.utc)
            self.assertEqual(due, SOON)
            self.assertIn("Testa, 2004 Testmake Fixture", task.title)
            self.assertIn(f"job #{self.ids.ro1}", task.title)
            self.assertNotIn("Example", task.title)  # first name only
            self.assertNotIn("0199", task.title)
            self.assertEqual(db.query(ImperialTaskAudit).filter_by(task_id=task.id).count(), 2)

    def test_cancel_and_complete_close_the_task(self):
        with patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"}), self.Session() as db:
            cancelled = self.appointment(db, self.ids.ro1)
            done = self.appointment(db, self.ids.ro2, start=SOON + timedelta(days=1))
            for item in (cancelled, done):
                appointment_followup.add_reminder(db, item, "user:1")
            appointment_followup.close_reminder(db, cancelled, "user:1", "cancelled")
            appointment_followup.close_reminder(db, done, "user:1", "completed")
            db.commit()
            states = {t.external_id: t.status for t in db.query(ImperialTask).filter_by(source="appointment")}
            self.assertEqual(states, {str(cancelled.id): "cancelled", str(done.id): "done_verified"})
            self.assertIn("Customer, Vehicle details not given yet",
                          db.query(ImperialTask).filter_by(external_id=str(done.id)).one().title)

    def test_tasks_switched_off_does_nothing(self):
        with patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": ""}), self.Session() as db:
            item = self.appointment(db, self.ids.ro1)
            self.assertIsNone(appointment_followup.add_reminder(db, item, "user:1"))
            self.assertIsNone(appointment_followup.close_reminder(db, item, "user:1", "cancelled"))
            self.assertEqual(db.query(ImperialTask).count(), 0)


class FeedTests(_Data):
    def setUp(self):
        super().setUp()
        self.feed_env = patch.dict(os.environ, {"APPOINTMENT_FEED_ENABLED": "true"})
        self.feed_env.start()

    def tearDown(self):
        self.feed_env.stop()
        super().tearDown()

    def test_feed_shows_first_name_vehicle_job_and_nothing_private(self):
        with self.Session() as db:
            confirmed = self.appointment(db, self.ids.ro1)
            tentative = self.appointment(db, self.ids.ro2, status="tentative", start=SOON + timedelta(days=1),
                                         location="mobile")
            self.appointment(db, self.ids.ro1, status="cancelled", start=SOON + timedelta(days=2))
            self.appointment(db, self.ids.ro1, start=SOON - timedelta(days=90))  # too old
            db.commit()
            text = appointment_feed.build(db, "https://shop.example.invalid")
        unfolded = text.replace("\r\n ", "")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertEqual(unfolded.count("BEGIN:VEVENT"), 2)
        self.assertIn(f"UID:appointment-{confirmed.id}@imperial-operations", unfolded)
        self.assertIn("SUMMARY:Testa\\, 2004 Testmake Fixture", unfolded)
        self.assertIn("SUMMARY:Not confirmed: Customer\\, Vehicle details not given yet", unfolded)
        self.assertIn("STATUS:TENTATIVE", unfolded)
        self.assertIn("LOCATION:Mobile visit", unfolded)
        self.assertIn(f"Job #{self.ids.ro1}", unfolded)
        self.assertIn("TRIGGER:-PT60M", unfolded)
        self.assertIn(SOON.strftime("DTSTART:%Y%m%dT%H%M%SZ"), unfolded)
        for private in ("0199", "Fixture Way", "fixture@", "Example", "Unknown"):
            self.assertNotIn(private, unfolded)
        self.assertTrue(all(len(line.encode()) <= 75 for line in text.split("\r\n")))

    def test_feed_shows_bay_job_lines_status_and_task_reminders(self):
        with self.Session() as db:
            db.add_all([LineItem(repair_order_id=self.ids.ro1, description="Timing chain kit", item_type="labor"),
                        LineItem(repair_order_id=self.ids.ro1, description="Water pump", item_type="labor",
                                 approval="declined"),
                        LineItem(repair_order_id=self.ids.ro1, description="Chain guide", item_type="part")])
            visit = self.appointment(db, self.ids.ro1)
            visit.resource_id = "bay-2"
            due = SOON + timedelta(hours=5)
            todo = ImperialTask(title="Call Testa back 602-555-0199 about parts", source="dashboard",
                                external_id="t1", status="open", due_at=due)
            done = ImperialTask(title="Old done thing", source="dashboard", external_id="t2",
                                status="done_verified", due_at=due)
            undated = ImperialTask(title="No time set", source="dashboard", external_id="t3", status="open")
            own = ImperialTask(title="Appointment reminder", source=appointment_followup.SOURCE,
                               external_id=str(visit.id), status="open", due_at=SOON)
            db.add_all([todo, done, undated, own])
            db.commit()
            text = appointment_feed.build(db, "https://shop.example.invalid")
        unfolded = text.replace("\r\n ", "")
        self.assertIn("SUMMARY:Testa\\, 2004 Testmake Fixture (Bay 2)", unfolded)
        self.assertIn(f"Job #{self.ids.ro1}: Timing chain kit", unfolded)
        self.assertIn("Status: Confirmed.", unfolded)
        self.assertIn("LOCATION:Imperial Auto Care\\, Bay 2", unfolded)
        self.assertNotIn("Water pump", unfolded)  # declined
        self.assertNotIn("Chain guide", unfolded)  # parts stay off
        self.assertEqual(unfolded.count("BEGIN:VEVENT"), 2)  # the visit and one task
        self.assertIn(f"UID:task-{todo.id}@imperial-operations", unfolded)
        self.assertIn("SUMMARY:Reminder: Call Testa back about parts", unfolded)
        self.assertIn(due.strftime("DTSTART:%Y%m%dT%H%M%SZ"), unfolded)
        self.assertIn("TRIGGER:-PT15M", unfolded)
        for private in ("0199", "Fixture Way", "Old done thing", "No time set", "Appointment reminder"):
            self.assertNotIn(private, unfolded)

    def test_bay_names(self):
        def bay(resource, location="shop"):
            return appointment_feed.bay_label(SimpleNamespace(resource_id=resource, location=location))
        self.assertEqual(bay("bay-1"), "Bay 1")
        self.assertEqual(bay("Bay 2"), "Bay 2")
        self.assertEqual(bay("bay-1", "mobile"), "Mobile")
        self.assertEqual(bay("lift-a"), "Lift A")

    def test_long_lines_fold_and_unfold_exactly(self):
        line = "DESCRIPTION:" + "é" * 120
        folded = appointment_feed._fold(line)
        self.assertTrue(all(len(part.encode()) <= 75 for part in folded.split("\r\n")))
        self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_owner_makes_address_once_old_one_stops_and_off_works(self):
        owner = self.sign_in()
        self.assertEqual(self.client.get("/calendar-feed/anything.ics").status_code, 404)
        page = owner.post("/settings/users/calendar-feed/new")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        url = next(part.split('"')[0] for part in page.text.split('value="') if part.startswith("http"))
        self.assertIn("/calendar-feed/", url)
        self.assertIn("webcal://", page.text)
        path = "/calendar-feed/" + url.rsplit("/calendar-feed/", 1)[1]
        got = self.client.get(path)
        self.assertEqual(got.status_code, 200)
        self.assertTrue(got.headers["content-type"].startswith("text/calendar"))
        self.assertIn("no-store", got.headers["cache-control"])
        with self.Session() as db:
            stored = db.get(appointment_feed.business_settings.BusinessSetting, "appointment_feed").value
        self.assertNotIn(path.rsplit("/", 1)[1][:-4], stored)  # only the hash is kept

        owner.post("/settings/users/calendar-feed/new")
        self.assertEqual(self.client.get(path).status_code, 404)  # replaced
        self.assertEqual(owner.post("/settings/users/calendar-feed/off").status_code, 303)
        self.assertNotIn("New Address", owner.get("/settings").text)

    def test_only_the_owner_manages_it_and_it_is_hidden_when_off(self):
        office = self.sign_in("office")
        self.assertEqual(office.post("/settings/users/calendar-feed/new").status_code, 403)
        self.assertNotIn("calendar-feed", office.get("/settings").text)
        self.assertIn("Make My Calendar Address", self.sign_in().get("/settings").text)
        with patch.dict(os.environ, {"APPOINTMENT_FEED_ENABLED": ""}):
            owner = self.sign_in()
            self.assertNotIn("calendar-feed", owner.get("/settings").text)
            self.assertEqual(owner.post("/settings/users/calendar-feed/new").status_code, 404)


class AssistantToolTests(_Data):
    def test_update_customer_renames_unknown_and_never_touches_phone(self):
        _, headers = self.issue()
        base = f"/internal/agent/v1/customers/{self.ids.unknown}"
        got = self.client.patch(base, headers=headers, json={"first_name": "Iris", "last_name": "Fixture",
                                                             "phone": "6025550000"})
        self.assertEqual(got.status_code, 200)
        body = got.json()
        self.assertTrue(body["changed"])
        self.assertEqual((body["customer"]["first_name"], body["customer"]["last_name"]), ("Iris", "Fixture"))
        self.assertEqual(body["customer"]["phone"], "6025550198")
        again = self.client.patch(base, headers=headers, json={"first_name": "Iris", "last_name": ""})
        self.assertFalse(again.json()["changed"])  # blank required name leaves it as is
        with self.Session() as db:
            self.assertEqual(db.get(Customer, self.ids.unknown).name_source, "staff")
            self.assertEqual([a.action for a in db.query(AgentAction)], ["customer.update"])
        self.assertEqual(self.client.patch("/internal/agent/v1/customers/99999", headers=headers,
                                           json={"first_name": "X"}).status_code, 404)
        _, read_only = self.issue(["customers:read"])
        self.assertEqual(self.client.patch(base, headers=read_only, json={"first_name": "X"}).status_code, 403)

    def test_open_job_checks_vehicle_owner_and_is_logged(self):
        _, headers = self.issue()
        got = self.client.post("/internal/agent/v1/jobs", headers=headers, json={
            "customer_id": self.ids.customer, "vehicle_id": self.ids.vehicle, "concern": "Leak check"})
        self.assertEqual(got.status_code, 201)
        self.assertEqual(got.json()["lines"], [])
        wrong = self.client.post("/internal/agent/v1/jobs", headers=headers, json={
            "customer_id": self.ids.unknown, "vehicle_id": self.ids.vehicle})
        self.assertEqual(wrong.status_code, 422)
        with self.Session() as db:
            self.assertEqual([a.action for a in db.query(AgentAction)], ["job.open"])

    def test_confirm_needs_its_own_scope(self):
        _, no_confirm = self.issue([s for s in agent_keys.SCOPES if s != "appointments:confirm"])
        self.assertEqual(self.client.post("/internal/agent/v1/appointments/1/confirm",
                                          headers=no_confirm).status_code, 403)

    def test_connector_lists_the_new_tools_by_scope(self):
        from app.agent_connector import TOOLS
        self.assertEqual(TOOLS["update_customer"][0], "customers:write")
        self.assertEqual(TOOLS["open_job"][0], "estimates:write")
        self.assertEqual(TOOLS["confirm_appointment"][0], "appointments:confirm")
        _, headers = self.issue(["customers:read", "customers:write"])
        listed = self.client.post("/internal/agent/mcp", headers=headers,
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        names = {t["name"] for t in listed["result"]["tools"]}
        self.assertIn("update_customer", names)
        self.assertNotIn("confirm_appointment", names)
        called = self.client.post("/internal/agent/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "update_customer", "arguments": {"customer_id": self.ids.unknown, "first_name": "Iris"}}})
        result = called.json()["result"]
        self.assertFalse(result["isError"])
        self.assertIn('"first_name": "Iris"', result["content"][0]["text"])
        self.assertIn('"last_name": "(0199)"', result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
