"""Made-up records for the Ask Imperial restart walkthrough: one customer per device who owns two cars."""
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
import app.assist_model  # noqa: F401
import app.business_settings  # noqa: F401
from app.appointment_model import Appointment  # noqa: F401
from app.dispatch_model import DispatchItem  # noqa: F401
from app.imperial_task_model import ImperialTask  # noqa: F401
from app.local_node_model import DiagnosticReview  # noqa: F401
from app.models import Customer, RepairOrder, User, Vehicle
from app.security import UserMfa  # noqa: F401

Base.metadata.create_all(engine)
db = SessionLocal()
for uid, name, role in ((1, "owner", "owner"), (2, "desk", "office"), (3, "crew", "tech")):
    db.add(User(id=uid, username=name, full_name=f"QA {name.title()}", password_hash=hash_password("ask-qa-only-pass"), role=role))
for cid, phone in ((11, "6025550411"), (13, "6025550413")):
    db.add(Customer(id=cid, first_name="Synthetic", last_name=f"Two-Cars {cid}", phone=phone))
    db.add(Vehicle(id=cid, customer_id=cid, year=2022, make="Dodge", model="Charger", engine="5.7L"))
    db.add(Vehicle(id=cid + 1, customer_id=cid, year=2016, make="Ford", model="Focus", engine="2.0L"))
    db.add(RepairOrder(id=cid, customer_id=cid, vehicle_id=cid, status="diagnosing", concern="Brakes grinding"))
    db.add(RepairOrder(id=cid + 1, customer_id=cid, vehicle_id=cid + 1, status="diagnosing", concern="Shudder from a stop"))
db.commit()
db.close()
print("seeded")
