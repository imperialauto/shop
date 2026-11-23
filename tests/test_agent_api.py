"""Assistant access keys and the assistant API. Made-up data only."""
import os
import secrets
import unittest
from datetime import timedelta
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_keys
from app import business_settings as store
from app.agent_keys import AgentAction, AgentKey
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Communication, Customer, Invoice, LineItem, RepairOrder, User, Vehicle
from app.security import limiter

PASSWORD = "fixture-password-1"
ALL = list(agent_keys.SCOPES)


class _Setup(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.store_patch = patch.object(store, "SessionLocal", self.Session)
        self.store_patch.start()
        store.clear_cache()
        self.env = patch.dict(os.environ, {"AGENT_API_ENABLED": "true"})
        self.env.start()
        self.read_only = app.state.read_only
        app.state.read_only = False
        limiter.clear("agent:ip:testclient")
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"))
            db.commit()
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        limiter.clear("agent:ip:testclient")
        self.env.stop()
        self.store_patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def issue(self, scopes=ALL, days=90):
        with self.Session() as db:
            key, raw = agent_keys.issue(db, 1, "Claude", scopes, days)
            return key.id, {"Authorization": f"Bearer {raw}"}

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client


class KeyTests(_Setup):
    def test_plain_key_is_never_stored(self):
        with self.Session() as db:
            key, raw = agent_keys.issue(db, 1, "Claude", ALL)
            self.assertTrue(raw.startswith("iak_"))
            self.assertGreater(len(raw), 40)
            row = db.get(AgentKey, key.id)
            self.assertNotIn(raw, (row.key_hash, row.key_prefix, row.scopes, row.name))
            self.assertEqual(row.key_hash, agent_keys.hash_key(raw))
            self.assertEqual(agent_keys.find(db, raw).id, key.id)

    def test_scopes_expiry_and_limit_are_checked(self):
        with self.Session() as db:
            with self.assertRaises(ValueError):
                agent_keys.issue(db, 1, "x", ["finance:write"])
            with self.assertRaises(ValueError):
                agent_keys.issue(db, 1, "x", ALL, days=10_000)
            key, _ = agent_keys.issue(db, 1, "x", ["customers:read", "made:up"])
            self.assertEqual(key.scope_list, ["customers:read"])
            for _ in range(agent_keys.MAX_ACTIVE_KEYS - 1):
                agent_keys.issue(db, 1, "x", ALL)
            with self.assertRaises(ValueError):
                agent_keys.issue(db, 1, "x", ALL)


class ApiAccessTests(_Setup):
    def test_feature_off_refuses_every_call(self):
        _, headers = self.issue()
        with patch.dict(os.environ, {"AGENT_API_ENABLED": "false"}):
            self.assertEqual(self.client.get("/internal/agent/v1/whoami", headers=headers).status_code, 503)

    def test_missing_wrong_revoked_and_expired_keys_are_refused(self):
        self.assertEqual(self.client.get("/internal/agent/v1/customers").status_code, 401)
        bad = {"Authorization": "Bearer iak_" + "x" * 43}
        self.assertEqual(self.client.get("/internal/agent/v1/customers", headers=bad).status_code, 401)
        key_id, headers = self.issue()
        self.assertEqual(self.client.get("/internal/agent/v1/whoami", headers=headers).status_code, 200)
        with self.Session() as db:
            self.assertTrue(agent_keys.revoke(db, key_id, 1))
        self.assertEqual(self.client.get("/internal/agent/v1/whoami", headers=headers).status_code, 401)
        key_id, headers = self.issue()
        with self.Session() as db:
            row = db.get(AgentKey, key_id)
            row.expires_at = agent_keys.utc_now() - timedelta(minutes=1)
            db.commit()
        self.assertEqual(self.client.get("/internal/agent/v1/whoami", headers=headers).status_code, 401)

    def test_x_agent_key_header_also_works_and_last_used_is_recorded(self):
        with self.Session() as db:
            key, raw = agent_keys.issue(db, 1, "Claude", ALL)
        self.assertEqual(self.client.get("/internal/agent/v1/whoami", headers={"X-Agent-Key": raw}).status_code, 200)
        with self.Session() as db:
            self.assertIsNotNone(db.get(AgentKey, key.id).last_used_at)

    def test_repeated_bad_keys_are_throttled(self):
        bad = {"Authorization": "Bearer iak_wrong"}
        codes = [self.client.get("/internal/agent/v1/whoami", headers=bad).status_code for _ in range(12)]
        self.assertIn(429, codes)

    def test_scope_is_enforced(self):
        _, headers = self.issue(scopes=["customers:read"])
        self.assertEqual(self.client.get("/internal/agent/v1/customers", headers=headers).status_code, 200)
        created = self.client.post("/internal/agent/v1/customers", headers=headers,
                                   json={"first_name": "Pat", "last_name": "Example"})
        self.assertEqual(created.status_code, 403)
        self.assertEqual(self.client.post("/internal/agent/v1/appointments", headers=headers, json={}).status_code, 403)

    def test_key_never_opens_staff_pages(self):
        _, headers = self.issue()
        for path in ("/dashboard", "/customers/", "/finance/", "/settings", "/invoices/"):
            response = self.client.get(path, headers={**headers, "accept": "text/html"})
            self.assertNotEqual(response.status_code, 200, path)
        self.assertNotIn("set-cookie", {k.lower() for k in self.client.get("/internal/agent/v1/whoami",
                                                                          headers=headers).headers})

    def test_read_only_mode_blocks_writes(self):
        _, headers = self.issue()
        app.state.read_only = True
        response = self.client.post("/internal/agent/v1/customers", headers=headers,
                                    json={"first_name": "Pat", "last_name": "Example"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get("/internal/agent/v1/customers", headers=headers).status_code, 200)


class ApiWorkTests(_Setup):
    def test_customer_vehicle_estimate_flow_is_logged_and_sends_nothing(self):
        _, headers = self.issue()
        made = self.client.post("/internal/agent/v1/customers", headers=headers, json={
            "first_name": "Pat", "last_name": "Example", "phone": "(602) 555-0100"}).json()
        self.assertFalse(made["existing"])
        customer_id = made["customer"]["id"]
        again = self.client.post("/internal/agent/v1/customers", headers=headers, json={
            "first_name": "Patricia", "last_name": "Example", "phone": "602-555-0100"}).json()
        self.assertTrue(again["existing"])
        self.assertEqual(again["customer"]["id"], customer_id)
        found = self.client.get("/internal/agent/v1/customers?q=6025550100", headers=headers).json()
        self.assertEqual([c["id"] for c in found], [customer_id])

        vehicle = self.client.post(f"/internal/agent/v1/customers/{customer_id}/vehicles", headers=headers, json={
            "year": 2003, "make": "Chevrolet", "model": "Suburban", "vin": "1gnek13z03j000000"}).json()
        self.assertEqual(vehicle["vehicle"]["vin"], "1GNEK13Z03J000000")
        vehicle_id = vehicle["vehicle"]["id"]

        job = self.client.post("/internal/agent/v1/estimates", headers=headers, json={
            "customer_id": customer_id, "vehicle_id": vehicle_id, "concern": "P0332 knock sensor",
            "lines": [{"description": "Replace knock sensors", "quantity": 2.7, "unit_price": 125.99},
                      {"description": "Knock sensor", "item_type": "part", "quantity": 2, "unit_price": 40}]})
        self.assertEqual(job.status_code, 201)
        body = job.json()
        self.assertEqual(len(body["lines"]), 2)
        self.assertTrue(all(line["approval"] is None for line in body["lines"]))
        more = self.client.post(f"/internal/agent/v1/jobs/{body['id']}/lines", headers=headers,
                                json=[{"description": "Intake gasket", "item_type": "part", "unit_price": 30}])
        self.assertEqual(len(more.json()["lines"]), 3)

        with self.Session() as db:
            actions = [a.action for a in db.query(AgentAction).order_by(AgentAction.id)]
            self.assertEqual(actions, ["customer.create", "vehicle.create", "estimate.create", "estimate.add_lines"])
            self.assertEqual(db.query(Communication).count(), 0)
            self.assertEqual(db.query(Customer).count(), 1)

    def test_vehicle_must_belong_to_customer(self):
        _, headers = self.issue()
        with self.Session() as db:
            a = Customer(first_name="A", last_name="One")
            b = Customer(first_name="B", last_name="Two")
            db.add_all([a, b])
            db.flush()
            v = Vehicle(customer_id=b.id, make="Ford")
            db.add(v)
            db.commit()
            a_id, v_id = a.id, v.id
        response = self.client.post("/internal/agent/v1/estimates", headers=headers,
                                    json={"customer_id": a_id, "vehicle_id": v_id})
        self.assertEqual(response.status_code, 422)

    def test_invoiced_job_lines_are_refused(self):
        _, headers = self.issue()
        with self.Session() as db:
            c = Customer(first_name="A", last_name="One")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, make="Ford")
            db.add(v)
            db.flush()
            ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, public_token="t" * 40)
            db.add(ro)
            db.flush()
            db.add(Invoice(repair_order_id=ro.id))
            db.commit()
            ro_id = ro.id
        response = self.client.post(f"/internal/agent/v1/jobs/{ro_id}/lines", headers=headers,
                                    json=[{"description": "Extra", "unit_price": 10}])
        self.assertEqual(response.status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(LineItem).count(), 0)
            self.assertEqual(db.query(AgentAction).count(), 0)


class JobStatusTests(_Setup):
    def make_job(self, status="intake", approval=None):
        with self.Session() as db:
            c = Customer(first_name="Sam", last_name="Example", phone="6025550111")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, year=2005, make="Dodge", model="Ram 3500")
            db.add(v)
            db.flush()
            ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, status=status, public_token=secrets.token_hex(20))
            db.add(ro)
            db.flush()
            db.add(LineItem(repair_order_id=ro.id, description="Transmission service", unit_price=180,
                            approval=approval))
            db.commit()
            return ro.id

    def move(self, headers, ro_id, status, note=None):
        body = {"status": status}
        if note is not None:
            body["note"] = note
        return self.client.post(f"/internal/agent/v1/jobs/{ro_id}/status", headers=headers, json=body)

    def test_moves_job_logs_it_and_sends_nothing(self):
        _, headers = self.issue()
        ro_id = self.make_job(status="approval_received", approval="approved")
        response = self.move(headers, ro_id, "In Progress", "parts in, starting the fluid service")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["status"], body["previous_status"], body["changed"]),
                         ("in_progress", "approval_received", True))
        self.assertIn("next_step", body)
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, ro_id).status, "in_progress")
            action = db.query(AgentAction).one()
            self.assertEqual((action.action, action.target_id), ("job.status", ro_id))
            self.assertEqual(action.summary, "approval_received -> in_progress; parts in, starting the fluid service")
            self.assertEqual(db.query(Communication).count(), 0)

    def test_same_status_changes_and_logs_nothing(self):
        _, headers = self.issue()
        ro_id = self.make_job(status="diagnosing")
        body = self.move(headers, ro_id, "diagnosing").json()
        self.assertFalse(body["changed"])
        with self.Session() as db:
            self.assertEqual(db.query(AgentAction).count(), 0)

    def test_job_page_checks_still_apply(self):
        _, headers = self.issue()
        ro_id = self.make_job(status="intake", approval=None)
        response = self.move(headers, ro_id, "in_progress")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Approve at least one", response.json()["detail"])
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, ro_id).status, "intake")
            self.assertEqual(db.query(AgentAction).count(), 0)

    def test_complete_and_picked_up_stay_with_the_office(self):
        from app.dispatch_model import DispatchItem, DispatchOutbound
        _, headers = self.issue()
        ro_id = self.make_job(status="in_progress", approval="approved")
        for target in ("complete", "delivered", "Complete"):
            response = self.move(headers, ro_id, target)
            self.assertEqual(response.status_code, 409, target)
        done = self.make_job(status="complete", approval="approved")
        self.assertEqual(self.move(headers, done, "in_progress").status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, ro_id).status, "in_progress")
            self.assertEqual(db.get(RepairOrder, done).status, "complete")
            self.assertEqual(db.query(AgentAction).count(), 0)
            self.assertEqual(db.query(DispatchItem).count(), 0)
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_unknown_status_missing_job_and_scope(self):
        _, headers = self.issue()
        ro_id = self.make_job()
        self.assertEqual(self.move(headers, ro_id, "paid").status_code, 422)
        self.assertEqual(self.move(headers, 9999, "diagnosing").status_code, 404)
        _, read_only = self.issue(scopes=["estimates:read"])
        self.assertEqual(self.move(read_only, ro_id, "diagnosing").status_code, 403)

    def test_allowed_statuses_are_board_columns(self):
        from app.routes import agent_api
        from app.routes.repair_orders import RO_STATUSES
        board = dict(RO_STATUSES)
        for key, label in agent_api.AGENT_STATUSES.items():
            self.assertEqual(board[key], label)
        self.assertEqual(set(board) - set(agent_api.AGENT_STATUSES), set(agent_api.OFFICE_ONLY_STATUSES))


class OwnerSettingsTests(_Setup):
    def test_owner_issues_key_once_and_revokes_it(self):
        owner = self.sign_in()
        page = owner.get("/settings")
        self.assertIn("Assistant Access", page.text)
        made = owner.post("/settings/users/assistant-keys/create",
                          data={"name": "Claude", "days": "90", "scopes": ["customers:read", "estimates:write"]})
        self.assertEqual(made.status_code, 200)
        self.assertEqual(made.headers.get("cache-control"), "no-store")
        raw = made.text.split('value="iak_', 1)[1].split('"', 1)[0]
        raw = "iak_" + raw
        self.assertNotIn(raw, owner.get("/settings").text)
        headers = {"Authorization": f"Bearer {raw}"}
        self.assertEqual(self.client.get("/internal/agent/v1/customers", headers=headers).status_code, 200)
        with self.Session() as db:
            key_id = db.query(AgentKey).one().id
        self.assertEqual(owner.post(f"/settings/users/assistant-keys/{key_id}/revoke").status_code, 303)
        self.assertEqual(self.client.get("/internal/agent/v1/customers", headers=headers).status_code, 401)

    def test_only_the_owner_manages_keys_and_section_hides_when_off(self):
        office = self.sign_in("office")
        self.assertEqual(office.post("/settings/users/assistant-keys/create",
                                     data={"scopes": ALL}).status_code, 403)
        with patch.dict(os.environ, {"AGENT_API_ENABLED": "false"}):
            owner = self.sign_in()
            self.assertNotIn("Assistant Access", owner.get("/settings").text)
            self.assertEqual(owner.post("/settings/users/assistant-keys/create",
                                        data={"scopes": ALL}).status_code, 404)

    def test_recovery_audit_ignores_tables_until_enabled(self):
        from app import recovery
        with patch.object(recovery, "engine", self.engine):
            Base.metadata.tables["agent_actions"].drop(self.engine)
            Base.metadata.tables["agent_keys"].drop(self.engine)
            with patch.dict(os.environ, {"AGENT_API_ENABLED": "false"}):
                self.assertNotIn("agent_keys", recovery.database_audit(include_counts=False)["missing_tables"])
            self.assertIn("agent_keys", recovery.database_audit(include_counts=False)["missing_tables"])


if __name__ == "__main__":
    unittest.main()
