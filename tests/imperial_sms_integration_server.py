"""Standalone synthetic integration server for ephemeral CI, never Railway.

The server refuses non-loopback fixture databases. No Quo, production, GitHub
credentials or customer records are used. GitHub Actions terminates this process
after Node exercises the real service-to-service API.
"""
import os
import tempfile
from pathlib import Path

workspace = tempfile.TemporaryDirectory(prefix="imperial-owner-sms-")
db_file = Path(workspace.name) / "qa.sqlite"
os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
os.environ["SECRET_KEY"] = "synthetic-sms-session-key-never-use-in-production"
os.environ["APP_READ_ONLY"] = "false"
os.environ["IMPERIAL_TASKS_ENABLED"] = "true"
os.environ["INTERNAL_API_KEY"] = "local-integration-fixture-key"
os.environ["OWNER_TEXTS_ENABLED"] = "false"
os.environ["AUTO_REPLIES_ENABLED"] = "false"
os.environ["PUBLIC_FORMS_ENABLED"] = "false"
os.environ.pop("QUO_API_KEY", None)
os.environ.pop("PROJECT_STATUS_GITHUB_TOKEN", None)

from app.database import Base, engine, SessionLocal
from app.imperial_task_model import ImperialTask  # registers task metadata
from app.models import User
from app.auth import hash_password
from app.main import app
import uvicorn

if __name__ == "__main__":
    assert db_file.parent.resolve() == Path(workspace.name).resolve()
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(username="sms-fixture-owner",
                    password_hash=hash_password("synthetic-owner-password"),
                    role="owner"))
        db.commit()
    uvicorn.run(app, host="127.0.0.1", port=18765, log_level="warning", access_log=False)
