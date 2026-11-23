"""Read-only list of recent customer approve/decline decisions for Claude. Made-up data only."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from test_agent_api import ALL, _Setup

from app.agent_keys import AgentAction
from app.models import Customer, LineItem, RepairOrder, Vehicle
from app.routes import agent_api

NOW = datetime(2026, 11, 22, 21, 0, tzinfo=timezone.utc)


class RecentDecisionTests(_Setup):
    def setUp(self):
        super().setUp()
        with self.Session() as db:
            c = Customer(first_name="Pat", last_name="Example", phone="6025550100")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, year=2013, make="Ford", model="Fusion")
            db.add(v)
            db.flush()
            ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="approval_received")
            old = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="approval_received")
            db.add_all([ro, old])
            db.flush()
            db.add_all([
                LineItem(repair_order_id=ro.id, description="Brake pads", quantity=1, unit_price=200,
                         approval="approved", approval_source="link", decided_at=NOW - timedelta(hours=1)),
                LineItem(repair_order_id=ro.id, description="Rotors", quantity=2, unit_price=75,
                         approval="declined", approval_source="link", decided_at=NOW - timedelta(hours=1)),
                LineItem(repair_order_id=ro.id, description="Wipers", quantity=1, unit_price=30, approval="pending"),
                LineItem(repair_order_id=ro.id, description="Oil change", quantity=1, unit_price=80,
                         approval="approved", approval_source="phone", approval_recorded_by="owner",
                         decided_at=NOW - timedelta(hours=2)),
                LineItem(repair_order_id=old.id, description="Battery", quantity=1, unit_price=180,
                         approval="approved", approval_source="text", decided_at=NOW - timedelta(days=3)),
            ])
            db.commit()
            self.ro_id, self.old_id = ro.id, old.id

    def test_customer_decisions_in_the_last_day(self):
        with self.Session() as db:
            got = agent_api.recent_decisions(db, now=NOW)
        self.assertEqual(got["count"], 1)
        job = got["decisions"][0]
        self.assertEqual(job["job_id"], self.ro_id)
        self.assertEqual(job["customer"]["name"], "Pat Example")
        self.assertEqual(job["vehicle"], "2013 Ford Fusion")
        self.assertEqual([l["description"] for l in job["approved"]], ["Brake pads"])
        self.assertEqual([l["description"] for l in job["declined"]], ["Rotors"])
        self.assertEqual(job["approved_total"], 200)
        self.assertEqual(job["still_waiting"], 1)

    def test_staff_recorded_and_older_ones_on_request(self):
        with self.Session() as db:
            staff = agent_api.recent_decisions(db, include_staff=True, now=NOW)
            week = agent_api.recent_decisions(db, since=NOW - timedelta(days=7), now=NOW)
            capped = agent_api.recent_decisions(db, since=NOW - timedelta(days=60), now=NOW)
        self.assertIn("Oil change", json.dumps(staff))
        self.assertEqual({d["job_id"] for d in week["decisions"]}, {self.ro_id, self.old_id})
        self.assertEqual(week["decisions"][0]["job_id"], self.ro_id)  # newest first
        self.assertEqual(capped["since"], (NOW - timedelta(days=14)).isoformat())

    def test_mcp_tool_is_read_only_and_needs_estimates_read(self):
        _, headers = self.issue(scopes=["estimates:read"])
        rpc = lambda method, params=None: self.client.post(
            "/internal/agent/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()
        tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
        self.assertTrue(tools["list_recent_decisions"]["annotations"]["readOnlyHint"])
        got = rpc("tools/call", {"name": "list_recent_decisions",
                                 "arguments": {"since": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()}})["result"]
        self.assertFalse(got["isError"], got)
        self.assertIn("decisions", json.loads(got["content"][0]["text"]))
        _, other = self.issue(scopes=["customers:read"])
        self.assertNotIn("list_recent_decisions", json.dumps(self.client.post(
            "/internal/agent/mcp", headers=other, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()))
        with self.Session() as db:
            self.assertEqual(db.query(AgentAction).count(), 0)

    def test_rest_route(self):
        _, headers = self.issue(scopes=ALL)
        got = self.client.get("/internal/agent/v1/decisions", headers=headers, params={"include_staff": "true"})
        self.assertEqual(got.status_code, 200, got.text)


if __name__ == "__main__":
    unittest.main()
