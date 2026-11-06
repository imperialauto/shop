"""Acceptance check against the private preview. Made-up data only. Reads the URL and codes from the environment."""
import json
import os
import random
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, CODE, PASSWORD = os.environ["PREVIEW_URL"].rstrip("/"), os.environ["PREVIEW_ACCESS_CODE"], os.environ["PREVIEW_OWNER_PASSWORD"]
OUT = Path("preview-output")
OUT.mkdir(exist_ok=True)
results = []


def step(device, name, action):
    try:
        action()
        results.append({"device": device, "step": name, "ok": True})
    except Exception as error:  # noqa: BLE001
        results.append({"device": device, "step": name, "ok": False, "error": str(error)[:300]})


def run(browser, device, options):
    context = browser.new_context(**options)
    page = context.new_page()
    phone = f"602555{random.randint(2000, 9999)}"
    state = {}

    def shot(name):
        page.screenshot(path=str(OUT / f"{device}-{name}.png"), full_page=True)

    def gate():
        response = page.goto(BASE + "/site/")
        assert "preview-access" in page.url, page.url
        assert "noindex" in response.headers.get("x-robots-tag", "")
        page.fill("[name=code]", "wrong-code")
        page.click("button[type=submit]")
        assert "did not match" in page.inner_text("body")
        page.fill("[name=code]", CODE)
        page.click("button[type=submit]")
        page.wait_for_url("**/site/")

    def pages():
        for path in ("/site/", "/site/services", "/site/services/towing", "/site/services/nvzn-customs", "/site/pricing", "/site/rentals", "/site/faq", "/site/status", "/site/contact"):
            assert page.goto(BASE + path).status == 200, path
            text = page.inner_text("body")
            assert "Suite 508" in text and "emergency roadside" in text, path
            assert "$" not in text, path
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 2, (path, overflow)
        page.goto(BASE + "/site/pricing")
        assert "Price coming soon" in page.inner_text("body")

    def fill_person(first):
        page.fill("[name=year]", "2016")
        page.fill("[name=make]", "Ford")
        page.fill("[name=model]", "Focus")
        page.fill("[name=first_name]", first)
        page.fill("[name=last_name]", "Preview")
        page.fill("[name=phone]", phone)

    def book():
        page.goto(BASE + "/site/book?service=pre-purchase")
        assert "Price coming soon" in page.inner_text("body")
        radio = page.locator("input[name=slot_id]").first
        state["slot"] = radio.get_attribute("value")
        page.locator("label.choice", has=radio).click()
        fill_person("Book" + device)
        page.click("button:has-text('Book this time')")
        page.wait_for_url("**/site/book/received")
        text = page.inner_text("body")
        assert "confirmed" in text and "Reference WEB-" in text and "$" not in text, text[:200]
        state["ref"] = re.search(r"WEB-\d+", text).group(0)
        shot("booked")

    def conflict():
        page.goto(BASE + "/site/book?service=pre-purchase")
        assert page.locator(f"input[name=slot_id][value='{state['slot']}']").count() == 0

    def saturday():
        page.goto(BASE + "/site/book?service=pre-purchase")
        days = [d for d in page.locator(".day h3").all_inner_texts() if d.startswith("Saturday")]
        assert days, "no Saturday times"
        day = page.locator(".day", has_text=days[0])
        labels = day.locator(".choice span").all_inner_texts()
        assert "12:00 PM" in labels and "3:00 PM" not in labels, labels  # 90 minutes plus an hour ends after 5

    def mobile():
        page.goto(BASE + "/site/book?service=battery-charging")
        page.locator("label.choice", has=page.locator("input[value=mobile]")).click()
        page.fill("[name=zip]", "85307")
        page.click("button:has-text('Update times')")
        assert "to be confirmed" in page.inner_text("body")
        page.locator("label.choice", has=page.locator("input[name=slot_id]")).last.click()
        page.fill("[name=street]", "100 Test St")
        fill_person("Mobile" + device)
        page.click("button:has-text('Book this time')")
        page.wait_for_url("**/site/book/received")
        shot("mobile-booked")

    def estimate(slug, where, words):
        def action():
            page.goto(f"{BASE}/site/estimate?service={slug}")
            page.fill("textarea[name=description]", words)
            page.locator("label.choice", has=page.locator(f"input[name=where][value={where}]")).click()
            if where != "shop":
                page.fill("[name=location]", "Made-up location, mile marker 1")
            page.fill("[name=make]", "Ford")
            page.fill("[name=model]", "Focus")
            page.fill("[name=first_name]", "Estimate" + device)
            page.fill("[name=last_name]", "Preview")
            page.fill("[name=phone]", phone)
            page.click("form[action='/site/estimate'] button[type=submit]")
            page.wait_for_url("**/site/estimate/received")
        return action

    def status():
        page.goto(BASE + "/site/status")
        page.fill("[name=reference]", state["ref"])
        page.fill("[name=phone]", "6025550000")
        page.click("form[action='/site/status'] button[type=submit]")
        assert "could not match" in page.inner_text("body")
        page.fill("[name=reference]", state["ref"])
        page.fill("[name=phone]", phone)
        page.click("form[action='/site/status'] button[type=submit]")
        page.wait_for_url("**/status/*")
        shot("status")

    def dashboard():
        page.goto(BASE + "/login")
        page.fill("[name=username]", "preview-owner")
        page.fill("[name=password]", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/dashboard")
        body = page.goto(BASE + "/ro/") and page.inner_text("body")
        assert "Book" + device in body and "Estimate" + device in body and "Mobile" + device in body, body[:200]
        appointments = page.goto(BASE + "/appointments/").text()
        assert '"confirmed"' in appointments and '"mobile"' in appointments, appointments[:200]
        page.goto(BASE + "/dashboard")
        shot("dashboard")

    for name, action in [("access code", gate), ("pages", pages), ("instant booking", book), ("conflict removed", conflict), ("Saturday fit", saturday),
                         ("mobile booking", mobile), ("complex estimate", estimate("diesel", "shop", "Made-up preview: hard start when cold.")),
                         ("roadside request", estimate("towing", "roadside", "Made-up preview: needs a tow from the shoulder.")),
                         ("status lookup", status), ("operations dashboard", dashboard)]:
        step(device, name, action)
    context.close()


with sync_playwright() as play:
    chromium = play.chromium.launch()
    run(chromium, "phone", play.devices["iPhone 13"])
    run(chromium, "desktop", {"viewport": {"width": 1280, "height": 800}})
    chromium.close()
(OUT / "report.json").write_text(json.dumps(results, indent=1))
print(json.dumps(results, indent=1))
sys.exit(1 if any(not r["ok"] for r in results) else 0)
