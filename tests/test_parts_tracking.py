"""Parts card on the job page: ordered, arrived, supplier, ETA, and the job moving on when parts are in.
Made-up data only."""
import unittest
from datetime import date, timedelta

from test_agent_api import PASSWORD, _Setup

from app import parts_tracking
from app.auth import hash_password
from app.dispatch_model import DispatchAudit, DispatchOutbound
from app.models import Communication, Customer, LineItem, PartsOrder, RepairOrder, User, Vehicle
from app.permissions import allowed


class PartsTrackingTests(_Setup):
    def setUp(self):
        super().setUp()
        with self.Session() as db:
            db.add(User(id=3, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            c = Customer(first_name="Pat", last_name="Example", phone="6025550100")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, year=2005, make="Cadillac", model="Escalade")
            db.add(v)
            db.flush()
            ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="waiting_parts", public_token="p" * 40)
            db.add(ro)
            db.flush()
            db.add(LineItem(repair_order_id=ro.id, description="Water pump", item_type="part", quantity=1,
                            unit_price=180, approval="approved"))
            db.commit()
            self.ro_id = ro.id
        self.owner = self.sign_in("owner")

    def add(self, client=None, **form):
        data = {"description": "Water pump", "part_number": "AW5104", "supplier": "AutoZone",
                "quantity": "1", "cost": "84.99", "eta": ""}
        data.update(form)
        return (client or self.owner).post(f"/ro/{self.ro_id}/parts/add", data=data)

    def parts(self):
        with self.Session() as db:
            return [(p.description, p.status, p.vendor, p.eta, p.received_at is not None)
                    for p in db.query(PartsOrder).order_by(PartsOrder.id)]

    def part_ids(self):
        with self.Session() as db:
            return [p.id for p in db.query(PartsOrder).order_by(PartsOrder.id)]

    def job_status(self):
        with self.Session() as db:
            return db.get(RepairOrder, self.ro_id).status

    def arrive(self, part_id, client=None):
        return (client or self.owner).post(f"/ro/{self.ro_id}/parts/{part_id}/arrived")

    def test_office_adds_a_part_with_supplier_and_eta(self):
        eta = date.today() + timedelta(days=2)
        response = self.add(client=self.sign_in("office"), eta=eta.isoformat())
        self.assertEqual(response.status_code, 302)
        self.assertIn("parts=added", response.headers["location"])
        self.assertEqual(self.parts(), [("Water pump", "ordered", "AutoZone", eta, False)])

    def test_bad_input_is_refused_and_nothing_saved(self):
        response = self.add(description="  ")
        self.assertIn("parts_error=", response.headers["location"])
        response = self.add(cost="abc")
        self.assertIn("parts_error=", response.headers["location"])
        self.assertEqual(self.parts(), [])

    def test_marking_the_only_part_arrived_moves_the_job_on_without_texting(self):
        self.add()
        response = self.arrive(self.part_ids()[0])
        self.assertIn("parts=moved", response.headers["location"])
        self.assertEqual(self.parts()[0][1], "received")
        self.assertTrue(self.parts()[0][4])
        self.assertEqual(self.job_status(), "in_progress")
        with self.Session() as db:
            audit = db.query(DispatchAudit).filter(DispatchAudit.action == "parts_in_job_moved").all()
            self.assertEqual(len(audit), 1)
            self.assertIn(f"job {self.ro_id}", audit[0].detail)
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
            self.assertEqual(db.query(Communication).count(), 0)

    def test_no_advance_while_another_part_is_still_ordered(self):
        self.add()
        self.add(description="Thermostat", supplier="O'Reilly")
        first, second = self.part_ids()
        response = self.arrive(first)
        self.assertIn("parts=arrived", response.headers["location"])
        self.assertEqual(self.job_status(), "waiting_parts")
        self.arrive(second)
        self.assertEqual(self.job_status(), "in_progress")

    def test_returned_parts_are_ignored(self):
        self.add()
        self.add(description="Wrong gasket")
        first, second = self.part_ids()
        self.arrive(first)
        self.assertEqual(self.job_status(), "waiting_parts")
        response = self.owner.post(f"/ro/{self.ro_id}/parts/{second}/returned")
        self.assertIn("parts=moved", response.headers["location"])
        self.assertEqual(self.job_status(), "in_progress")

    def test_only_returned_parts_do_not_count_as_all_here(self):
        with self.Session() as db:
            self.assertFalse(parts_tracking.all_arrived([PartsOrder(status="returned")]))
            self.assertFalse(parts_tracking.all_arrived([PartsOrder(status="sourcing")]))
            self.assertTrue(parts_tracking.all_arrived([PartsOrder(status="received"), PartsOrder(status="returned"),
                                                        PartsOrder(status="sourcing")]))

    def test_arrival_on_a_job_not_waiting_on_parts_leaves_status_alone(self):
        with self.Session() as db:
            db.get(RepairOrder, self.ro_id).status = "diagnosing"
            db.commit()
        self.add()
        self.arrive(self.part_ids()[0])
        self.assertEqual(self.job_status(), "diagnosing")

    def test_tech_can_mark_arrived_but_not_add_or_return(self):
        self.add()
        part_id = self.part_ids()[0]
        tech = self.sign_in("tech")
        self.assertEqual(self.add(client=tech).status_code, 403)
        self.assertEqual(tech.post(f"/ro/{self.ro_id}/parts/{part_id}/returned").status_code, 403)
        self.assertEqual(tech.post(f"/ro/{self.ro_id}/parts/{part_id}/edit", data={"description": "x"}).status_code, 403)
        self.assertEqual(tech.post(f"/ro/{self.ro_id}/parts/hold").status_code, 403)
        response = self.arrive(part_id, client=tech)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.job_status(), "in_progress")
        self.assertTrue(allowed("tech", "POST", f"/ro/{self.ro_id}/parts/{part_id}/arrived"))
        self.assertFalse(allowed("tech", "POST", f"/ro/{self.ro_id}/parts/add"))

    def test_part_from_another_job_is_refused(self):
        with self.Session() as db:
            other = RepairOrder(customer_id=1, vehicle_id=1, status="waiting_parts", public_token="q" * 40)
            db.add(other)
            db.flush()
            db.add(PartsOrder(repair_order_id=other.id, description="Belt", status="ordered"))
            db.commit()
            stray = db.query(PartsOrder).one().id
        self.assertEqual(self.arrive(stray).status_code, 404)

    def test_hold_for_parts_is_offered_not_forced(self):
        with self.Session() as db:
            db.get(RepairOrder, self.ro_id).status = "approval_received"
            db.commit()
        self.add()
        self.assertEqual(self.job_status(), "approval_received")
        page = self.owner.get(f"/ro/{self.ro_id}").text
        self.assertIn("Hold for parts", page)
        response = self.owner.post(f"/ro/{self.ro_id}/parts/hold")
        self.assertIn("parts=held", response.headers["location"])
        self.assertEqual(self.job_status(), "waiting_parts")
        self.arrive(self.part_ids()[0])
        self.assertEqual(self.job_status(), "in_progress")

    def test_page_shows_the_card_with_cost_for_office_only(self):
        late = date.today() - timedelta(days=3)
        self.add(eta=late.isoformat())
        page = self.owner.get(f"/ro/{self.ro_id}")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn('id="parts"', page.text)
        self.assertIn("AW5104", page.text)
        self.assertIn("AutoZone", page.text)
        self.assertIn("84.99", page.text)
        self.assertIn("(late)", page.text)
        self.assertIn("+ Add a part", page.text)
        tech_page = self.sign_in("tech").get(f"/ro/{self.ro_id}")
        self.assertEqual(tech_page.status_code, 200, tech_page.text[:300])
        self.assertIn("Water pump", tech_page.text)
        self.assertIn(">Arrived</button>", tech_page.text)
        self.assertNotIn("84.99", tech_page.text)
        self.assertNotIn("+ Add a part", tech_page.text)

    def test_job_waiting_on_parts_with_none_ordered_is_flagged(self):
        page = self.owner.get(f"/ro/{self.ro_id}").text
        self.assertIn("no part is on order here", page)

    def test_sourced_part_can_be_marked_ordered(self):
        with self.Session() as db:
            db.add(PartsOrder(repair_order_id=self.ro_id, description="Fuel tank", status="sourcing", cost=81.83))
            db.commit()
        part_id = self.part_ids()[0]
        self.owner.post(f"/ro/{self.ro_id}/parts/{part_id}/ordered")
        self.assertEqual(self.parts()[0][1], "ordered")
        self.arrive(part_id)
        self.assertEqual(self.job_status(), "in_progress")


if __name__ == "__main__":
    unittest.main()
