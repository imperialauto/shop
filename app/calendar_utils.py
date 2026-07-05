"""
Google Calendar integration — service account approach.

Setup:
1. Go to console.cloud.google.com → New project → Enable "Google Calendar API"
2. IAM & Admin → Service Accounts → Create → download JSON key
3. base64-encode the JSON: python -c "import base64; print(base64.b64encode(open('key.json','rb').read()).decode())"
4. Add GOOGLE_SERVICE_ACCOUNT_JSON=<base64> and GOOGLE_CALENDAR_ID=<your_calendar_id> to Railway
5. Share your Google Calendar with the service account email (give it "Make changes to events" permission)
"""

import base64
import json
import datetime
from app.config import GOOGLE_CALENDAR_ID, GOOGLE_SERVICE_ACCOUNT_JSON

PHOENIX_TZ = "America/Phoenix"


def create_appointment(
    summary: str,
    description: str,
    start_dt: datetime.datetime,
    duration_hours: float = 2.0,
) -> str | None:
    """
    Create a Google Calendar event. Returns the event URL on success, None on failure/not configured.
    start_dt should be a naive datetime in Phoenix local time.
    """
    if not GOOGLE_CALENDAR_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[Calendar] Not configured — GOOGLE_CALENDAR_ID or GOOGLE_SERVICE_ACCOUNT_JSON missing")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_info = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON).decode("utf-8"))

        credentials = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )

        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

        end_dt = start_dt + datetime.timedelta(hours=duration_hours)

        event_body = {
            "summary": summary,
            "description": description,
            "start": {
                "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": PHOENIX_TZ,
            },
            "end": {
                "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": PHOENIX_TZ,
            },
        }

        result = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
        link = result.get("htmlLink")
        print(f"[Calendar] Event created: {link}")
        return link

    except ImportError:
        print("[Calendar] google-api-python-client not installed")
        return None
    except Exception as e:
        print(f"[Calendar] Error creating event: {e}")
        return None
