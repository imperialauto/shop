"""Real failure patterns replayed through intake. All messages are synthetic."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.routes import internal_messaging

HEADERS = {"x-internal-key": "fixture-internal-key"}
INTAKE = "/internal/dispatch/intake"
# (message, status, urgency): every customer message must end up in front of the owner.
SCENARIOS = [
    ("I'm here", "needs_owner", "urgent"),
    ("?", "needs_owner", "urgent"),
    ("Voicemail: Hi, I'm at your front door", "needs_owner", "urgent"),
    ("we just pulled up", "needs_owner", "urgent"),
    ("nobody is here", "needs_owner", "urgent"),
    ("[Customer sent a photo]", "needs_owner", "normal"),
    ("See you another day. Ty", "needs_owner", "normal"),
    ("Tuesday morning. What time do you open?", "needs_owner", "normal"),
    ("How much for brakes on a 2011 Ford Focus?", "needs_owner", "normal"),
    ("my 2013 Ford Fusion has a fuel leak", "needs_owner", "safety"),
    ("truck overheated and I'm stranded on I-10", "needs_owner", "safety"),
    ("can you tow it today", "needs_owner", "urgent"),
]
REACTIONS = ["​👍​ to “ See you Tuesday ”", "​👍🏾​ to “ Perfect, 11 AM appointment ”",
             "Liked “See you then”", "Loved “Thanks”"]


class DayToDayTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        patcher = patch.object(internal_messaging, "INTERNAL_API_KEY", "fixture-internal-key")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def post(self, ident, body, conversation, **extra):
        return self.client.post(INTAKE, headers=HEADERS, json={
            "conversationId": conversation, "messageId": ident, "phone": "+16025550100", "body": body, **extra}).json()

    def test_no_customer_message_goes_quiet(self):
        for number, (body, status, urgency) in enumerate(SCENARIOS):
            with self.subTest(body):
                result = self.post(f"m{number}", body, f"CN{number}")
                self.assertEqual(result["status"], status)
                with self.Session() as db:
                    self.assertEqual(db.get(DispatchItem, result["itemId"]).urgency, urgency)

    def test_known_customers_are_never_asked_for_a_vehicle(self):
        for number, (body, _, _) in enumerate(SCENARIOS):
            with self.subTest(body):
                result = self.post(f"k{number}", body, f"CK{number}", autoRepliesOn=True, knownCustomer=True)
                self.assertNotIn("year, make", (result["autoReply"] or {}).get("text", ""))

    def test_reactions_change_nothing(self):
        for number, body in enumerate(REACTIONS):
            with self.subTest(body):
                self.assertTrue(self.post(f"r{number}", body, f"CR{number}", autoRepliesOn=True)["reaction"])
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).count(), 0)


if __name__ == "__main__":
    unittest.main()
