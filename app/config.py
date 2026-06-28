import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENPHONE_API_KEY = os.getenv("OPENPHONE_API_KEY", "")
OPENPHONE_SIGNING_SECRET = os.getenv("OPENPHONE_SIGNING_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./imperial_auto.db")

# Railway gives a postgres:// URL, SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
