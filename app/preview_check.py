"""Live check of a disposable preview through its public address. Made-up data only.

Runs in the preview's own container so its access code never leaves Railway.
It needs PREVIEW_RUN_CHECK=true and does nothing outside preview mode.
"""
import os
import random
import re
import threading
import time

import httpx

from app.public_site import preview_code, preview_mode

PAGES = ("/site/", "/site/services", "/site/services/towing", "/site/services/nvzn-customs", "/site/pricing",
         "/site/rentals", "/site/faq", "/site/status", "/site/contact", "/site/estimate")


def log(ok: bool, name: str, detail: str = ""):
    print(f"PREVIEW_CHECK {'PASS' if ok else 'FAIL'} {name} {detail}"[:240], flush=True)


def slots(client, query):
    return re.findall(r'name="slot_id" value="(\d+)"', client.get(f"/site/book?{query}").text)


def person(phone, first):
    return {"first_name": first, "last_name": "Preview", "phone": phone, "contact_method": "text", "make": "Ford", "model": "Focus", "year": "2016"}


def run(base: str):
    phone = f"602555{random.randint(2000, 9999)}"
    client = httpx.Client(base_url=base, timeout=30)
    state = {}

    def gate():
        first = client.get("/site/")
        assert first.status_code == 303 and first.headers["location"] == "/preview-access"
        assert client.post("/preview-access", data={"code": "wrong"}).status_code == 403
        assert client.post("/preview-access", data={"code": preview_code()}).status_code == 303
        assert "noindex" in client.get("/site/").headers["x-robots-tag"]

    def pages():
        for path in PAGES:
            page = client.get(path)
            assert page.status_code == 200, path
            assert "Suite 508" in page.text and "emergency roadside" in page.text and "$" not in re.sub(r"<script.*?</script>", "", page.text, flags=re.S), path
        assert "Price coming soon" in client.get("/site/pricing").text

    def booking():
        open_times = slots(client, "service=pre-purchase")
        state["slot"], state["next"] = open_times[0], str(int(open_times[0]) + 1)
        form = {"service": "pre-purchase", "where": "shop", "zip": "", "slot_id": state["slot"], **person(phone, "Booker")}
        assert client.post("/site/book", data=form).status_code == 303
        received = client.get("/site/book/received").text
        assert "confirmed" in received and "$" not in received.split("site-footer")[0]
        state["ref"] = re.search(r"WEB-\d+", received).group(0)
        assert client.post("/site/book", data={**form, "phone": "6025551999"}).status_code == 409

    def conflict():
        open_times = slots(client, "service=diagnostic")
        assert state["slot"] not in open_times and state["next"] not in open_times

    def saturday():
        text = client.get("/site/book?service=pre-purchase").text
        match = re.search(r"<h3>Saturday.*?</div>\s*</div>", text, re.S)
        assert match and "12:00 PM" in match.group(0) and "3:00 PM" not in match.group(0)

    def mobile():
        query = "service=battery-charging&where=mobile&zip=85307"
        assert "to be confirmed" in client.get(f"/site/book?{query}").text
        form = {"service": "battery-charging", "where": "mobile", "zip": "85307", "street": "100 Test St",
                "slot_id": slots(client, query)[-1], **person(phone[:-1] + "1", "Mobiler")}
        assert client.post("/site/book", data=form).status_code == 303

    def estimate(line, where, first):
        form = {"service_line": line, "where": where, "location": "" if where == "shop" else "Made-up location",
                "description": "Made-up preview request for testing.", **person(phone[:-1] + str(len(first) % 10), first)}
        assert client.post("/site/estimate", data=form).status_code == 303

    def status():
        assert client.post("/site/status", data={"reference": state["ref"], "phone": "6025550000"}).status_code == 404
        found = client.post("/site/status", data={"reference": state["ref"], "phone": phone})
        assert found.status_code == 303 and client.get(found.headers["location"]).status_code == 200

    def dashboard():
        login = client.post("/login", data={"username": "preview-owner", "password": os.environ["PREVIEW_OWNER_PASSWORD"]})
        assert login.status_code == 302
        orders = client.get("/ro/").text
        for name in ("Booker", "Mobiler", "Estimator", "Roadside"):
            assert name in orders, name
        rows = client.get("/appointments/").json()
        shop = next(r for r in rows if r["location"] == "shop")
        away = next(r for r in rows if r["location"] == "mobile")
        assert (shop["status"], shop["buffer_after_minutes"]) == ("confirmed", 60)
        assert (away["travel_before_minutes"], away["buffer_after_minutes"]) == (30, 30)

    for name, action in (("access code", gate), ("pages", pages), ("instant booking", booking), ("conflict and buffer", conflict),
                         ("Saturday fit", saturday), ("mobile booking", mobile),
                         ("complex estimate", lambda: estimate("diesel", "shop", "Estimator")),
                         ("roadside request", lambda: estimate("towing", "roadside", "Roadside")),
                         ("status lookup", status), ("operations dashboard", dashboard)):
        try:
            action()
            log(True, name)
        except Exception as error:  # noqa: BLE001
            log(False, name, f"{type(error).__name__} {str(error)[:120]}")
    print("PREVIEW_CHECK DONE", flush=True)


def start():
    if not preview_mode() or os.getenv("PREVIEW_RUN_CHECK") != "true":
        return
    domain = os.environ["RAILWAY_PUBLIC_DOMAIN"]

    def later():
        time.sleep(20)
        run(f"https://{domain}")
    threading.Thread(target=later, daemon=True).start()
