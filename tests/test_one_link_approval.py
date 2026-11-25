"""One customer link: the owner's Text link now button, the inspection checklist and photos shared
by default. Made-up data, private database, no live services; nothing is texted."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import customer_file
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound
from app.main import app
from app.models import Customer, LineItem, RepairOrder, RoAttachment, RoInspectionItem, User, Vehicle
from app.sending_pause import PAUSED

PASSWORD = "fixture-password-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def thread(path, phone):
    assert (path, phone) == ("/conversation", "6025550100")
    return {"messages": [{"id": "AC1", "direction": "INBOUND"}, {"id": "AC2", "direction": "OUTBOUND"}]}, None


def newer(path, phone):
    return {"messages": [{"id": "AC2"}, {"id": "AC3"}]}, None


class OneLinkTests(unittest.TestCase):
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
        with self.Session() as db:
            for uid, name, role in ((1, "owner", "owner"), (2, "office", "office"), (3, "tech", "tech")):
                db.add(User(id=uid, username=name, password_hash=hash_password(PASSWORD), role=role))
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"))
            db.add(Customer(id=2, first_name="Lee", last_name="Nophone", phone=""))
            db.add(Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"))
            db.add(Vehicle(id=2, customer_id=2, year=2012, make="Honda", model="Fit"))
            db.add(RepairOrder(id=5, customer_id=1, vehicle_id=1, status="diagnosing", public_token="made-up-token"))
            db.add(RepairOrder(id=6, customer_id=2, vehicle_id=2, status="diagnosing", public_token="other-token"))
            db.add_all([LineItem(repair_order_id=5, description="Front pads", unit_price=349),
                        LineItem(repair_order_id=5, description="Flush", unit_price=99),
                        LineItem(repair_order_id=6, description="Battery", unit_price=180)])
            db.commit()
        self.clients = {}

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def client(self, name="owner"):
        if name not in self.clients:
            c = TestClient(app, follow_redirects=False)
            c.post("/login", data={"username": name, "password": PASSWORD})
            self.clients[name] = c
        return self.clients[name]

    def text_now(self, who="owner", ro=5, fetch=thread):
        with patch.object(customer_file, "_bridge", fetch):
            return self.client(who).post(f"/ro/{ro}/text-link-now")

    def outbound(self):
        with self.Session() as db:
            return [(o.kind, o.status, o.item_id) for o in db.query(DispatchOutbound).order_by(DispatchOutbound.id)]

    def test_owner_button_queues_the_link_text_once_with_no_yes_text(self):
        r = self.text_now()
        self.assertEqual(r.status_code, 303)
        with self.Session() as db:
            item = db.query(DispatchItem).one()
            self.assertEqual((item.status, item.category, item.customer_phone, item.last_message_id),
                             ("approved", "approval_link", "+16025550100", "AC2"))
            self.assertIn("/status/made-up-token", item.draft_reply)
            self.assertIn("Hi Pat", item.draft_reply)
            self.assertEqual({li.approval for li in db.get(RepairOrder, 5).line_items}, {"pending"})
            self.assertEqual(db.get(RepairOrder, 5).status, "waiting_approval")
        self.assertIn(f"link=sending{item.id}", r.headers["location"])
        self.assertEqual(self.outbound(), [("dashboard_reply", "queued", item.id)])

    def test_offer_snap_box_adds_the_snap_line_and_no_link(self):
        from app import snap_offer
        with patch.object(customer_file, "_bridge", thread):
            self.client().post("/ro/5/text-link-now", data={"offer_snap": "1"})
        with self.Session() as db:
            text = db.query(DispatchItem).one().draft_reply
        self.assertIn(snap_offer.LINE + " - Imperial Auto Care", text)
        self.assertEqual(text.count("http"), 1)  # only the approval link, never a Snap link

    def test_second_press_before_anything_changes_does_not_text_twice(self):
        self.text_now()
        r = self.text_now()
        self.assertIn("link=already", r.headers["location"])
        self.assertEqual(len(self.outbound()), 1)

    def test_new_customer_message_after_send_allows_a_fresh_link(self):
        self.text_now()
        self.text_now(fetch=newer)
        self.assertEqual(len(self.outbound()), 2)

    def test_only_owner_or_admin_can_text_from_the_page(self):
        self.assertEqual(self.text_now("office").status_code, 403)
        self.assertEqual(self.text_now("tech").status_code, 403)
        self.assertEqual(self.outbound(), [])

    def test_stopped_texting_sends_nothing(self):
        with self.Session() as db:
            db.add(DispatchAudit(actor="owner", action=PAUSED))
            db.commit()
        self.assertIn("link=paused", self.text_now().headers["location"])
        self.assertEqual(self.outbound(), [])

    def test_no_phone_and_bridge_down_send_nothing(self):
        self.assertIn("link=no_phone", self.text_now(ro=6).headers["location"])
        self.assertIn("link=unavailable", self.text_now(fetch=lambda p, n: (None, "unavailable")).headers["location"])
        self.assertEqual(self.outbound(), [])

    def test_nothing_waiting_sends_nothing(self):
        with self.Session() as db:
            for li in db.get(RepairOrder, 5).line_items:
                li.approval = "approved"
            db.commit()
        self.assertIn("link=nothing", self.text_now().headers["location"])
        self.assertEqual(self.outbound(), [])

    def test_deleting_the_job_right_after_cancels_the_waiting_link_text(self):
        from app import ro_delete
        self.text_now()
        self.client().post("/ro/5/inspection", data={"cond_0": "good"})
        with self.Session() as db:
            ro_delete.delete_repair_order(db, db.get(RepairOrder, 5), "owner")
        self.assertEqual([s for _, s, _ in self.outbound()], ["skipped"])
        with self.Session() as db:
            self.assertEqual(db.query(RoInspectionItem).count(), 0)
            self.assertEqual(db.query(DispatchItem).one().status, "closed")

    def test_owner_ask_for_approval_texts_the_link_straight_away(self):
        # Ask for approval (home page, job page step) is the owner's yes too: no queue, no YES text.
        with patch.object(customer_file, "_bridge", thread):
            r = self.client().post("/ro/5/request-approval")
        with self.Session() as db:
            item = db.query(DispatchItem).one()
            self.assertEqual(item.status, "approved")
            self.assertEqual(db.get(RepairOrder, 5).status, "waiting_approval")
        self.assertIn(f"link=sending{item.id}", r.headers["location"])
        self.assertEqual(self.outbound(), [("dashboard_reply", "queued", item.id)])
        with patch.object(customer_file, "_bridge", thread):
            again = self.client().post("/ro/5/request-approval")
        self.assertIn("link=already", again.headers["location"])
        self.assertEqual(len(self.outbound()), 1)

    def test_office_ask_for_approval_still_waits_for_the_owner(self):
        with patch.object(customer_file, "_bridge", thread):
            r = self.client("office").post("/ro/5/request-approval")
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).one().status, "needs_owner")
        self.assertIn("link=queued", r.headers["location"])
        self.assertEqual(self.outbound(), [])

    def test_link_already_texted_outside_the_dashboard_is_not_sent_again(self):
        def texted(path, phone):
            return {"messages": [{"id": "AC1", "direction": "INBOUND", "body": "how much?"},
                                 {"id": "AC2", "direction": "OUTBOUND",
                                  "body": "Your quote: https://example.test/sign/ro/made-up-token Jae"}]}, None

        def replied(path, phone):
            return {"messages": texted(path, phone)[0]["messages"] + [
                {"id": "AC3", "direction": "INBOUND", "body": "can you resend?"}]}, None

        r = self.text_now(fetch=texted)
        self.assertIn("link=texted", r.headers["location"])
        self.assertEqual(self.outbound(), [])
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 5).status, "waiting_approval")  # still marked as waiting on them
        # Once they text back, a fresh link may go out.
        self.assertIn("link=sending", self.text_now(fetch=replied).headers["location"])
        self.assertEqual(len(self.outbound()), 1)

    def test_a_failed_send_can_be_tried_again(self):
        self.text_now()
        with self.Session() as db:
            db.query(DispatchOutbound).one().status = "failed"
            db.query(DispatchItem).one().status = "closed"
            db.commit()
        self.assertIn("link=sending", self.text_now().headers["location"])
        self.assertEqual([s for _, s, _ in self.outbound()], ["failed", "queued"])

    def test_owner_sees_the_button_office_does_not(self):
        self.assertIn("/ro/5/text-link-now", self.client("owner").get("/ro/5").text)
        self.assertNotIn("/ro/5/text-link-now", self.client("office").get("/ro/5").text)

    def test_inspection_saves_and_shows_worst_first_on_the_customer_link(self):
        r = self.client("tech").post("/ro/5/inspection", data={
            "cond_0": "good", "note_0": "7/32 all around", "cond_1": "now", "note_1": "Front pads at 2 mm",
            "cond_2": "watch", "extra_area": "Cabin air filter", "extra_cond": "watch", "extra_note": "Dusty"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("inspection=4", r.headers["location"])
        page = TestClient(app).get("/status/made-up-token").text
        self.assertIn("Inspection", page)
        self.assertLess(page.index("Front pads at 2 mm"), page.index("7/32 all around"))
        self.assertIn("Cabin air filter", page)
        self.assertNotIn("Belts and hoses", page)
        staff = self.client("owner").get("/ro/5").text
        self.assertIn("Front pads at 2 mm", staff)
        self.assertIn('value="Cabin air filter"', staff)

    def test_saving_again_replaces_the_checklist_and_other_jobs_are_untouched(self):
        self.client().post("/ro/6/inspection", data={"cond_0": "good"})
        self.client().post("/ro/5/inspection", data={"cond_0": "now"})
        self.client().post("/ro/5/inspection", data={"cond_1": "good"})
        with self.Session() as db:
            rows = [(r.repair_order_id, r.area, r.condition) for r in db.query(RoInspectionItem).order_by(RoInspectionItem.repair_order_id)]
        self.assertEqual(rows, [(5, "Brakes", "good"), (6, "Tires and tread", "good")])

    def test_bad_condition_is_refused(self):
        self.assertEqual(self.client().post("/ro/5/inspection", data={"cond_0": "great"}).status_code, 422)

    def test_inspection_needs_staff_sign_in(self):
        r = TestClient(app, follow_redirects=False).post("/ro/5/inspection", data={"cond_0": "good"})
        self.assertEqual(r.status_code, 302)
        with self.Session() as db:
            self.assertEqual(db.query(RoInspectionItem).count(), 0)

    def test_photos_default_to_customer_for_the_office_and_stay_internal_for_techs(self):
        page = self.client("owner").get("/ro/5").text
        self.assertIn('name="visibility" value="customer" checked', page)  # Show customer starts ticked
        self.client("owner").post("/ro/5/attachments", files={"files": ("a.png", PNG, "image/png")}, data={"visibility": "customer"})
        self.client("tech").post("/ro/5/attachments", files={"files": ("b.png", PNG, "image/png")})
        with self.Session() as db:
            seen = {a.filename: a.visibility for a in db.query(RoAttachment)}
        self.assertEqual(seen, {"a.png": "customer", "b.png": "internal"})
        self.assertIn("Photos from the shop", TestClient(app).get("/status/made-up-token").text)


if __name__ == "__main__":
    unittest.main()
