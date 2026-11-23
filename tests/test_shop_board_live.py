"""Shop TV board, live parts: cars here, coming in, waiting on you, parts, customer alerts and the assistant's tools."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_connector, agent_keys, board_agent, business_settings, shop_board
from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchAudit, DispatchItem
from app.imperial_task_model import BOARD_CATEGORY, ImperialTask
from app.main import app
from app.models import Customer, LineItem, PartsOrder, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=PHOENIX)


class BoardLiveTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.env = patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true", "SHOP_BOARD_ALERTS": "true"})
        self.env.start()
        self.clock = patch.object(shop_board, "now_local", return_value=NOW)
        self.clock.start()
        self.read_only = app.state.read_only
        app.state.read_only = False
        business_settings.clear_cache()
        shop_board.register_alert_hooks()
        shop_board._last_reconcile[0] = float("inf")  # tests call reconcile themselves
        old = (NOW - timedelta(days=3)).astimezone(timezone.utc).replace(tzinfo=None)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=3, username="mike", password_hash=hash_password(PASSWORD), role="tech"))
            pat = Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550100")
            placeholder = Customer(id=2, first_name="2013 Ford Explorer", last_name="Overheating", phone="+16025550101")
            sam = Customer(id=3, first_name="Sam", last_name="Fixture", phone="+16025550102")
            db.add_all([pat, placeholder, sam])
            db.flush()
            db.add_all([Vehicle(id=1, customer_id=1, year=2005, make="Dodge", model="Ram"),
                        Vehicle(id=2, customer_id=2, year=2013, make="Ford", model="Explorer"),
                        Vehicle(id=3, customer_id=3, year=2004, make="Cadillac", model="Escalade"),
                        Vehicle(id=4, customer_id=3, year=2013, make="Ford", model="Fusion")])
            db.flush()
            db.add_all([
                RepairOrder(id=10, customer_id=1, vehicle_id=1, status="intake", created_at=old),          # here, not started
                RepairOrder(id=12, customer_id=2, vehicle_id=2, status="diagnosing", created_at=old),      # started: here
                RepairOrder(id=14, customer_id=3, vehicle_id=3, status="intake"),                          # booked, not here
                RepairOrder(id=7, customer_id=3, vehicle_id=4, status="waiting_approval", created_at=old),
                RepairOrder(id=11, customer_id=3, vehicle_id=4, status="waiting_approval", created_at=old),  # duplicate car
            ])
            db.flush()
            db.add(Appointment(repair_order_id=14, resource_id="bay-1", status="tentative",
                               starts_at=(NOW + timedelta(days=1)).astimezone(timezone.utc),
                               ends_at=(NOW + timedelta(days=1, hours=1)).astimezone(timezone.utc)))
            db.add(LineItem(repair_order_id=12, description="Head gasket", approval="pending"))
            db.add(PartsOrder(repair_order_id=12, vendor="AutoZone", description="Thermostat", cost=48.5, status="ordered"))
            db.add(PartsOrder(repair_order_id=12, vendor="AutoZone", description="Radiator cap", status="received"))
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.read_only
        self.env.stop()
        self.clock.stop()
        shop_board._last_reconcile[0] = 0.0
        business_settings.clear_cache()
        self.engine.dispose()

    def client(self, name="owner"):
        client = TestClient(app)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    # ---------------------------------------------------------------- cars here
    def test_only_cars_marked_here_or_started_show_and_each_car_once(self):
        phone = self.client()
        with self.Session() as db:
            self.assertEqual([j["id"] for j in shop_board.jobs_today(db, NOW)], [12])
        for ro in (10, 11):
            self.assertEqual(phone.post(f"/board/lot/{ro}", data={"operation": "here"}, follow_redirects=False).status_code, 303)
        with self.Session() as db:
            jobs = shop_board.jobs_today(db, NOW)
            statuses = {o.id: o.status for o in db.query(RepairOrder)}
        self.assertEqual(sorted(j["id"] for j in jobs), [10, 11, 12])
        self.assertEqual({j["id"]: j["days"] for j in jobs}[12], 3)
        self.assertEqual({j["id"]: j["days"] for j in jobs}[10], 0)  # marked here today
        self.assertEqual({j["id"]: j["stage"] for j in jobs}[11], "Waiting on approval")
        self.assertEqual(statuses[10], "intake")  # the job itself is untouched
        phone.post("/board/lot/12", data={"operation": "gone"})
        with self.Session() as db:
            self.assertNotIn(12, [j["id"] for j in shop_board.jobs_today(db, NOW)])
        manage = phone.get("/board/manage").text
        self.assertIn("Cars here", manage)
        self.assertIn("2004 Cadillac Escalade", manage)

    def test_placeholder_customer_names_stay_off_the_tv(self):
        with self.Session() as db:
            job = next(j for j in shop_board.jobs_today(db, NOW) if j["id"] == 12)
        self.assertEqual(job["who"], "")

    def test_a_booked_car_not_here_yet_is_coming_in(self):
        with self.Session() as db:
            coming = shop_board.coming_in(db, NOW)
        self.assertEqual([(c["day"], c["vehicle"], c["who"], c["tentative"]) for c in coming],
                         [("Tomorrow", "2004 Cadillac Escalade", "Sam", True)])

    # ---------------------------------------------------------------- waiting on you and parts
    def test_waiting_on_you_counts_texts_calls_and_estimates(self):
        with self.Session() as db:
            db.add(DispatchItem(quo_conversation_id="c1", customer_phone="+16025550100", status="needs_owner",
                                last_message_id="m1", approval_reasons='["missed_call"]'))
            db.add(DispatchItem(quo_conversation_id="c2", customer_phone="+16025550102", status="needs_owner",
                                last_message_id="m2"))
            db.add(DispatchItem(quo_conversation_id="c3", customer_phone="+16025550103", status="routine",
                                last_message_id="m3"))
            db.commit()
            waiting = shop_board.waiting_on_you(db)
        self.assertEqual(waiting, {"texts": 1, "calls": 1, "estimates": 3, "total": 5})

    def test_parts_on_order_show_without_prices(self):
        with self.Session() as db:
            parts = shop_board.parts_coming(db)
        self.assertEqual([(p["part"], p["vehicle"], p["vendor"]) for p in parts],
                         [("Thermostat", "2013 Ford Explorer", "AutoZone")])
        panel = self.client().get("/board/panel").text
        self.assertIn("Thermostat", panel)
        self.assertNotIn("48.5", panel)

    # ---------------------------------------------------------------- timers
    def test_timer_starts_from_the_phone_shows_on_the_tv_and_flags_time_up(self):
        phone = self.client("mike")
        with patch.object(shop_board, "now_local", return_value=datetime.now(PHOENIX)):
            r = phone.post("/board/timers", data={"label": "Explorer pressure test", "minutes": "15"}, follow_redirects=False)
            self.assertEqual(r.headers["location"], "/board/manage?saved=timer#timers")
            panel = phone.get("/board/panel").text
            self.assertIn("Explorer pressure test", panel)
            self.assertIn('class="tv-timer "', panel)
            with self.Session() as db:
                (timer,) = shop_board.timers(db, datetime.now(PHOENIX))
                self.assertTrue(895 <= timer["left"] <= 900)
                later = shop_board.timers(db, datetime.now(PHOENIX) + timedelta(minutes=16))
                self.assertTrue(later[0]["up"])
                self.assertEqual(shop_board.timers(db, datetime.now(PHOENIX) + timedelta(minutes=50)), [])  # drops off
            phone.post(f"/board/timers/{timer['id']}", data={"operation": "plus5"})
            with self.Session() as db:
                self.assertEqual(shop_board.timers(db, datetime.now(PHOENIX))[0]["minutes"], 20)
            phone.post(f"/board/timers/{timer['id']}", data={"operation": "stop"})
            self.assertNotIn("Explorer pressure test", phone.get("/board/panel").text)

    def test_custom_minutes_and_bad_input(self):
        phone = self.client()
        phone.post("/board/timers", data={"label": "Brake bleed", "minutes": "", "custom": "7"})
        with self.Session() as db:
            self.assertEqual([t["minutes"] for t in shop_board.timers(db, datetime.now(PHOENIX))], [7])
        bad = phone.post("/board/timers", data={"label": "x", "minutes": "0"}, follow_redirects=False)
        self.assertIn("error=", bad.headers["location"])
        bad = phone.post("/board/timers", data={"label": "x", "custom": "abc"}, follow_redirects=False)
        self.assertIn("error=", bad.headers["location"])

    # ---------------------------------------------------------------- pickup, waiting on customer, book time
    def test_ready_for_pickup_banner_uses_first_name_only(self):
        with self.Session() as db:
            db.get(RepairOrder, 12).status = "complete"
            db.get(Customer, 2).first_name = "Lee"
            db.commit()
        panel = self.client().get("/board/panel").text
        self.assertIn("Ready for pickup", panel)
        self.assertIn("Lee · 2013 Ford Explorer", panel)
        self.assertNotIn("Overheating", panel)

    def test_waiting_on_customer_lists_approval_jobs_with_how_long(self):
        with self.Session() as db:
            db.get(RepairOrder, 11).updated_at = (NOW - timedelta(days=3)).astimezone(timezone.utc).replace(tzinfo=None)
            db.commit()
            rows = shop_board.waiting_on_customer(db, NOW)
        self.assertEqual([(r["id"], r["who"]) for r in rows], [(11, "Sam")])  # one per car: the newer Fusion job
        self.assertEqual(rows[0]["wait"], "3 days")

    def test_job_clock_shows_time_worked_against_book_time(self):
        with self.Session() as db:
            db.add(LineItem(repair_order_id=12, description="Diag", item_type="labor", quantity=1.0))
            db.commit()
        phone = self.client("mike")
        start = NOW - timedelta(hours=2)
        with patch.object(shop_board, "now_local", return_value=start):
            phone.post("/board/clock/12", data={"operation": "start"})
        with self.Session() as db:
            job = {j["id"]: j for j in shop_board.jobs_today(db, NOW)}[12]
        self.assertEqual((job["book"], job["clock_on"]), (2.0, True))  # head gasket line defaults to 1 hr + diag 1 hr
        self.assertAlmostEqual(job["worked"], 2.0, places=1)
        with patch.object(shop_board, "now_local", return_value=NOW + timedelta(minutes=30)):
            phone.post("/board/clock/12", data={"operation": "stop"})
        with self.Session() as db:
            job = {j["id"]: j for j in shop_board.jobs_today(db, NOW + timedelta(hours=5))}[12]
        self.assertEqual((job["worked"], job["clock_on"], job["over_book"]), (2.5, False, True))
        self.assertIn("over book", self.client().get("/board/panel").text)
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 12).status, "diagnosing")  # the job itself is untouched

    # ---------------------------------------------------------------- customer alerts
    def _alert(self, **fields):
        with self.Session() as db:
            fields = {"quo_conversation_id": "c9", "last_message_id": "m9", **fields}
            item = DispatchItem(customer_phone="+16025550102", customer_id=3, vehicle_id=3,
                                status="needs_owner", summary="Private message text",
                                contact_name="Sam Fixture full", **fields)
            db.add(item)
            db.commit()
            return item.id

    def _alert_todo(self, db, item_id):
        return db.query(ImperialTask).filter_by(source=shop_board.SOURCE_ALERT, external_id=f"item:{item_id}").one_or_none()

    def test_an_alert_that_needs_jae_lands_on_the_board_once_and_masked(self):
        item_id = self._alert(approval_reasons='["missed_call"]')
        with self.Session() as db:
            item = db.get(DispatchItem, item_id)
            item.summary = "Another text"  # a later update makes no second to-do
            db.commit()
            todos = db.query(ImperialTask).filter_by(category=BOARD_CATEGORY).all()
        self.assertEqual([t.title for t in todos], ["Missed call: Sam, 2004 Cadillac Escalade"])
        panel = self.client().get("/board/panel").text
        for secret in ("+16025550102", "Private message", "Sam Fixture"):
            self.assertNotIn(secret, panel)

    def test_safety_alert_is_urgent(self):
        item_id = self._alert(urgency="safety", category="towing")
        with self.Session() as db:
            todo = self._alert_todo(db, item_id)
        self.assertEqual((todo.title.split(":")[0], todo.priority), ("Safety text", "urgent"))

    def test_handled_alert_moves_to_completed_and_comes_back_if_it_needs_jae_again(self):
        item_id = self._alert(category="booking")
        with self.Session() as db:
            db.get(DispatchItem, item_id).status = "approved"
            db.commit()
            self.assertEqual(self._alert_todo(db, item_id).status, shop_board.DONE)
            db.get(DispatchItem, item_id).status = "needs_owner"
            db.commit()
            db.expire_all()
            self.assertEqual(self._alert_todo(db, item_id).status, "open")

    def test_ticking_an_alert_on_the_board_clears_it_from_needs_attention(self):
        item_id = self._alert()
        with self.Session() as db:
            todo_id = self._alert_todo(db, item_id).id
        self.assertEqual(self.client("mike").post(f"/board/todos/{todo_id}", data={"operation": "done"}).status_code, 200)
        with self.Session() as db:
            self.assertEqual(db.get(DispatchItem, item_id).status, "needs_owner")  # techs can't clear customer alerts
        owner = self.client()
        owner.post(f"/board/todos/{todo_id}", data={"operation": "done"})
        with self.Session() as db:
            self.assertEqual(db.get(DispatchItem, item_id).status, "closed")
            self.assertEqual(db.query(DispatchAudit).filter_by(item_id=item_id, action="board_done").count(), 1)
            self.assertEqual(db.get(ImperialTask, todo_id).status, shop_board.DONE)
        undo = owner.post(f"/board/todos/{todo_id}", data={"operation": "undo"})
        self.assertIn("comes back by itself", undo.text)

    def test_nothing_mirrors_while_the_switch_is_off(self):
        with patch.dict(os.environ, {"SHOP_BOARD_ALERTS": ""}):
            item_id = self._alert()
            with self.Session() as db:
                self.assertIsNone(self._alert_todo(db, item_id))
                self.assertEqual(shop_board.reconcile_alerts(db), {"added": 0, "done": 0, "reopened": 0})

    def test_catch_up_pass_fills_in_and_clears_missed_alerts(self):
        with patch.dict(os.environ, {"SHOP_BOARD_ALERTS": ""}):
            item_id = self._alert(category="estimate")
        with self.Session() as db:
            self.assertEqual(shop_board.reconcile_alerts(db)["added"], 1)
            self.assertEqual(shop_board.reconcile_alerts(db)["added"], 0)
            with patch.dict(os.environ, {"SHOP_BOARD_ALERTS": ""}):
                db.get(DispatchItem, item_id).status = "closed"
                db.commit()
            self.assertEqual(shop_board.reconcile_alerts(db)["done"], 1)

    def test_old_backlog_stays_off_when_alerts_turn_on_but_new_activity_lands(self):
        since = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)
        before = since - timedelta(days=2)
        with patch.dict(os.environ, {"SHOP_BOARD_ALERTS": ""}):
            old_id = self._alert(category="estimate", created_at=before, updated_at=before)
        with patch.dict(os.environ, {"SHOP_BOARD_ALERTS_SINCE": "2026-11-23T01:00:00Z"}):
            with self.Session() as db:
                self.assertEqual(shop_board.reconcile_alerts(db)["added"], 0)
                self.assertIsNone(self._alert_todo(db, old_id))
                item = db.get(DispatchItem, old_id)  # the same customer texts again
                item.summary, item.updated_at = "New text", since + timedelta(minutes=5)
                db.commit()
                self.assertIsNotNone(self._alert_todo(db, old_id))
            new_id = self._alert(category="booking", quo_conversation_id="c10", last_message_id="m10", created_at=since + timedelta(minutes=9),
                                 updated_at=since + timedelta(minutes=9))
            with self.Session() as db:
                self.assertIsNotNone(self._alert_todo(db, new_id))

    def test_a_board_problem_never_breaks_message_intake(self):
        with patch.object(shop_board, "sync_alert", side_effect=RuntimeError("boom")):
            item_id = self._alert()
        with self.Session() as db:
            self.assertEqual(db.get(DispatchItem, item_id).status, "needs_owner")

    # ---------------------------------------------------------------- the assistant's tools
    def _key(self, scopes="board"):
        with self.Session() as db:
            key = agent_keys.AgentKey(name="Claude", key_prefix="k", key_hash="h" + scopes, scopes=scopes, created_by_user_id=1,
                                      expires_at=datetime.now(timezone.utc) + timedelta(days=30))
            db.add(key)
            db.commit()
            return SimpleNamespace(id=key.id, scope_list=key.scope_list)

    def _call(self, key, name, args):
        import json
        with self.Session() as db:
            result = agent_connector._call_tool(db, key, {"name": name, "arguments": args})
        return json.loads(result["content"][0]["text"]), result["isError"]

    def test_tasks_page_sync_tools_add_once_tick_and_list(self):
        key = self._key()
        first, err = self._call(key, "add_board_todo", {"title": "Call AutoZone about the cap", "task_key": "task-42"})
        self.assertFalse(err)
        again, _ = self._call(key, "add_board_todo", {"title": "Call AutoZone about the cap", "task_key": "task-42"})
        self.assertEqual((first["created"], again["created"], again["todo"]["id"]), (True, False, first["todo"]["id"]))
        done, _ = self._call(key, "set_board_todo", {"task_key": "task-42", "done": True})
        self.assertTrue(done["changed"])
        listing, _ = self._call(key, "list_board_todos", {})
        self.assertEqual([(t["task_key"], t["kind"], t["done"]) for t in listing["completed"]], [("task-42", "task", True)])
        self.assertTrue(listing["completed"][0]["done_at"])
        with self.Session() as db:
            actions = [a.action for a in db.query(agent_keys.AgentAction).order_by(agent_keys.AgentAction.id)]
        self.assertEqual(actions, ["board_todo_added", "board_todo_done"])

    def test_board_tools_need_the_board_permission(self):
        key = self._key("customers:read")
        self.assertNotIn("add_board_todo", [t["name"] for t in agent_connector._tool_list(key)])
        payload, err = self._call(key, "add_board_todo", {"title": "Sweep"})
        self.assertTrue(err)

    def test_set_tool_needs_an_id_or_key(self):
        payload, err = self._call(self._key(), "set_board_todo", {"done": True})
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
