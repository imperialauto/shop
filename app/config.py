import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # kept for backward compat
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENPHONE_API_KEY = os.getenv("OPENPHONE_API_KEY", "")
OPENPHONE_SIGNING_SECRET = os.getenv("OPENPHONE_SIGNING_SECRET", "")
OWNER_PHONE = os.getenv("OWNER_PHONE", "")  # Jaelan's cell — receives estimate approval texts
OPENPHONE_PHONE_NUMBER_ID = os.getenv("OPENPHONE_PHONE_NUMBER_ID", "")  # Primary inbox ID (PN9UGB5eVf) — bypasses API lookup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./imperial_auto.db")

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")  # Calendar ID or 'primary'
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # base64-encoded service account JSON
GOOGLE_REVIEW_LINK = os.getenv("GOOGLE_REVIEW_LINK", "https://g.page/r/review")  # Your GBP review shortlink

# Railway gives a postgres:// URL, SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
