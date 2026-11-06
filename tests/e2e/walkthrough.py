"""Clicks through the app like a user on a phone and a computer. Made-up data only."""
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
OUT = Path("walkthrough-output")
OUT.mkdir(exist_ok=True)
results = []
PAGES = [("dashboard", "/dashboard"), ("dashboard-urgent", "/dashboard?view=urgent"), ("repair-orders", "/ro/"), ("board", "/ro/board"),
         ("ro-new", "/ro/new"), ("ro-detail", "/ro/1"), ("estimates", "/estimates/"), ("invoices", "/invoices/"), ("invoice-detail", "/invoices/1"),
         ("finance", "/finance/"), ("customers", "/customers/"), ("fleet", "/customers/?type_filter=fleet"), ("missing-page", "/ro/99999"), ("customer-new", "/customers/new"),
         ("customer-detail", "/customers/1"), ("vehicle-new", "/vehicles/new?customer_id=1"), ("vehicle-detail", "/vehicles/1"),
         ("calendar", "/appointments/calendar"), ("settings", "/settings"), ("live-control", "/live/control"),
         ("site-home", "/site/"), ("site-services", "/site/services"), ("site-diesel", "/site/services/diesel"),
         ("site-nvzn", "/site/services/nvzn-customs"), ("site-contact", "/site/contact"), ("site-book", "/site/book"),
         ("site-book-times", "/site/book?service=diagnostic"), ("site-estimate", "/site/estimate")]


def measure(page, label, device, status, errors):
    metrics = page.evaluate("""() => {
      const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const inView = (el) => { const r = el.getBoundingClientRect(); return r.right > 0 && r.left < innerWidth; };
      const targets = [...document.querySelectorAll('a,button,select,input:not([type=hidden]),textarea')].filter(visible).filter(inView);
      const small = targets.filter(el => el.getBoundingClientRect().height < 40).map(el => (el.innerText || el.name || el.tagName).trim().slice(0, 30));
      const unreadable = [...document.querySelectorAll('input,select,textarea')].filter(visible).filter(el => parseFloat(getComputedStyle(el).fontSize) < 16).length;
      return { overflow: document.documentElement.scrollWidth - innerWidth, title: document.title, h1: (document.querySelector('.page-title,h1') || {}).innerText || '',
               smallTargets: small.length, smallSample: [...new Set(small)].slice(0, 8), zoomingInputs: unreadable, height: document.documentElement.scrollHeight };
    }""")
    results.append({"device": device, "page": label, "status": status, "errors": errors[:6], **metrics})
    page.screenshot(path=str(OUT / f"{device}-{label}.png"), full_page=True)


def visit(page, device, label, path):
    errors = []
    listener = lambda m: errors.append(m.text[:160]) if m.type == "error" else None
    page.on("console", listener)
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    response = page.goto(BASE + path, wait_until="networkidle")
    measure(page, label, device, response.status if response else 0, errors)
    page.remove_listener("console", listener)


def step(name, device, action):
    try:
        action()
        results.append({"device": device, "flow": name, "ok": True})
    except Exception as error:  # keep going so one failure does not hide the rest
        results.append({"device": device, "flow": name, "ok": False, "error": str(error)[:400]})


def flows(page, device):
    page_options = DEVICE_OPTIONS[device]
    context = {}

    def login():
        page.goto(BASE + "/login")
        page.fill("[name=username]", "walkthrough")
        page.fill("[name=password]", "walkthrough-only-pass")
        page.click("button[type=submit]")
        page.wait_for_url("**/dashboard")

    def customer():
        page.goto(BASE + "/customers/new")
        page.fill("[name=first_name]", "Flow" + device)
        page.fill("[name=last_name]", "Check")
        page.fill("[name=phone]", "6025550199")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")
        context["customer"] = int(re.search(r"/customers/(\d+)", page.url).group(1))

    def vehicle():
        page.goto(f"{BASE}/vehicles/new?customer_id={context['customer']}")
        page.select_option("[name=customer_id]", str(context["customer"]))
        page.select_option("[name=year]", "2018")
        page.fill("[name=make]", "Honda")
        page.fill("[name=model]", "Accord")
        page.click("form button[type=submit]")
        page.wait_for_load_state("networkidle")
        context["after_vehicle"] = page.url

    def repair_order():
        page.goto(f"{BASE}/ro/new?customer_id={context['customer']}")
        page.select_option("[name=customer_id]", str(context["customer"]))
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(600)
        options = page.eval_on_selector_all("[name=vehicle_id] option", "o => o.map(x => x.value).filter(Boolean)")
        page.select_option("[name=vehicle_id]", options[0])
        page.fill("[name=concern]", "Check engine light")
        page.click("form[action='/ro/new'] button[type=submit]")
        page.wait_for_url(re.compile(r"/ro/\d+$"))
        context["ro"] = int(page.url.rsplit("/", 1)[1])

    def line_item():
        page.fill("#li-description", "Diagnostic")
        page.fill("[name=quantity]", "1")
        page.fill("[name=unit_price]", "150")
        page.click(f"form[action='/ro/{context['ro']}/add-line-item'] button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "Diagnostic" in page.content()

    def part_item():
        page.fill("#li-description", "Oil filter")
        page.select_option("[name=item_type]", "part")
        page.fill("[name=quantity]", "1")
        page.fill("[name=unit_price]", "100")
        page.click(f"form[action='/ro/{context['ro']}/add-line-item'] button[type=submit]")
        page.wait_for_load_state("networkidle")

    def status():
        with page.expect_navigation():
            page.select_option("[name=status]", "complete")
        page.wait_for_load_state("networkidle")
        assert page.input_value("[name=status]") == "complete"

    def invoice():
        page.click(f"form[action='/invoices/create/{context['ro']}'] button[type=submit]")
        page.wait_for_url(re.compile(r"/invoices/\d+$"))
        context["invoice"] = int(page.url.rsplit("/", 1)[1])
        text = page.inner_text("body")
        assert "$9.20" in text and "$259.20" in text, "tax should be 9.2% of the $100 part only"

    def duplicate_invoice():
        page.goto(f"{BASE}/ro/{context['ro']}")
        assert page.locator(f"form[action='/invoices/create/{context['ro']}']").count() == 0, "RO still offers a second invoice"
        assert page.locator(f"a[href='/invoices/{context['invoice']}']").count() >= 1, "no link to the invoice"

    def payment():
        page.goto(f"{BASE}/invoices/{context['invoice']}")
        send = page.locator(f"form[action='/invoices/{context['invoice']}/mark-sent'] button")
        if send.count():
            send.click()
            page.wait_for_load_state("networkidle")
        page.select_option("[name=method]", index=0)
        page.locator(f"form[action='/invoices/{context['invoice']}/add-payment'] button[type=submit]").click()
        page.wait_for_load_state("networkidle")
        assert "Paid" in page.inner_text("body")

    def public_status():
        page.goto(f"{BASE}/ro/1")
        link = page.locator("a[href*='/status/']")
        assert link.count(), "no customer status link on the RO"
        page.goto(link.first.get_attribute("href") if link.first.get_attribute("href").startswith("http") else BASE + link.first.get_attribute("href"))
        measure(page, "public-status", device, 200, [])

    def wrong_password():
        page.goto(BASE + "/login")
        page.fill("[name=username]", "walkthrough")
        page.fill("[name=password]", "wrong-password")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "Invalid" in page.inner_text("body")

    def logged_out_redirect():
        fresh = page.context.browser.new_context().new_page()
        fresh.goto(BASE + "/ro/")
        assert "/login" in fresh.url, fresh.url
        fresh.context.close()

    def tech_blocked_from_finance():
        tech = page.context.browser.new_context().new_page()
        tech.goto(BASE + "/login")
        tech.fill("[name=username]", "tech")
        tech.fill("[name=password]", "walkthrough-only-pass")
        tech.click("button[type=submit]")
        tech.wait_for_url("**/dashboard")
        assert tech.locator("a[href='/finance/']").count() == 0
        assert tech.goto(BASE + "/finance/").status == 403
        tech.context.close()

    def search():
        page.goto(BASE + "/customers/?search=Testcase")
        assert "Lee" in page.inner_text("body") and "Pat Sample" not in page.inner_text("body")

    def queue_reply():
        page.goto(BASE + "/dashboard?view=urgent")
        before = page.locator("button[value=approve]").count()
        name = "Sample Contact 1" if device == "phone" else "Sample Contact 2"
        if device == "desktop":
            page.goto(BASE + "/dashboard")
            before = page.locator("button[value=approve]").count()
        page.locator("details summary", has_text=name).first.click()
        page.locator("details[open] textarea[name=reply_text]").first.fill("Edited test reply")
        page.locator("details[open] button[value=approve]").first.click()
        page.wait_for_load_state("networkidle")
        page.goto(BASE + ("/dashboard?view=urgent" if device == "phone" else "/dashboard"))
        assert page.locator("button[value=approve]").count() < before, "item still waiting after sending"

    def signature():
        page.goto(f"{BASE}/ro/{context['ro']}")
        link = page.locator("a[href*='/sign/ro/']")
        assert link.count(), "no signature link"
        href = link.first.get_attribute("href")
        page.goto(href if href.startswith("http") else BASE + href)
        page.fill("[name=approved_by]", "Flow Check")
        box = page.locator("#sig-canvas").bounding_box()
        page.mouse.move(box["x"] + 20, box["y"] + 40)
        page.mouse.down()
        page.mouse.move(box["x"] + 150, box["y"] + 90, steps=6)
        page.mouse.up()
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        measure(page, "signed", device, 200, [])
        assert "sign" in page.inner_text("body").lower() or "thank" in page.inner_text("body").lower()

    def booking():
        day = "2027-01-05" if device == "phone" else "2027-01-06"
        page.goto(f"{BASE}/ro/{context['ro']}")
        page.click("a[href*='/appointments/calendar?ro=']")
        page.wait_for_load_state("networkidle")
        assert page.input_value("[name=repair_order_id]") == str(context["ro"])
        page.fill("[name=starts_at]", f"{day}T11:00")
        page.fill("[name=ends_at]", f"{day}T12:00")
        page.click("#appointment-form button[type=submit]")
        page.wait_for_function("document.getElementById('booking-message').textContent.includes('saved')")
        page.locator("#calendar-rows tr", has_text=f"#{context['ro']}").first.wait_for(timeout=8000)
        page.fill("[name=starts_at]", f"{day}T12:30")
        page.fill("[name=ends_at]", f"{day}T13:30")
        page.click("#appointment-form button[type=submit]")
        page.wait_for_function("document.getElementById('booking-message').textContent.includes('already')")
        page.screenshot(path=str(OUT / f"{device}-booked.png"), full_page=True)

    def site_booking():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/")
        visitor.click("a[href='/site/book']")
        visitor.click("a[href='/site/book?service=diagnostic']")
        visitor.locator("label.choice", has=visitor.locator("input[name=slot_id]")).first.click()
        visitor.fill("[name=year]", "2016")
        visitor.fill("[name=make]", "Ford")
        visitor.fill("[name=model]", "Focus")
        visitor.fill("[name=first_name]", "Web" + device)
        visitor.fill("[name=last_name]", "Visitor")
        visitor.fill("[name=phone]", "6025550123")
        visitor.fill("[name=notes]", "Made-up walkthrough booking")
        visitor.click("form[action='/site/book'] button[type=submit]")
        visitor.wait_for_url("**/site/book/received")
        assert "Reference WEB-" in visitor.inner_text("body")
        measure(visitor, "site-booked", device, 200, [])
        visitor.context.close()
        page.goto(BASE + "/appointments/")
        assert "tentative" in page.inner_text("body"), "online booking not on the staff list"

    def site_estimate():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/services/diesel")
        visitor.click("a[href='/site/estimate?service=diesel']")
        assert visitor.input_value("[name=service_line]") == "diesel"
        visitor.fill("[name=description]", "Made-up walkthrough: hard start when cold")
        visitor.fill("[name=vin]", "1FTFW1ET5DFC10312")
        visitor.fill("[name=first_name]", "Est" + device)
        visitor.fill("[name=last_name]", "Visitor")
        visitor.fill("[name=phone]", "6025550124")
        visitor.click("form[action='/site/estimate'] button[type=submit]")
        visitor.wait_for_url("**/site/estimate/received")
        measure(visitor, "site-estimate-received", device, 200, [])
        visitor.context.close()

    for name, action in [("wrong password", wrong_password), ("login", login), ("public site booking", site_booking), ("public site estimate", site_estimate), ("new customer", customer), ("new vehicle", vehicle), ("new repair order", repair_order),
                         ("add line item", line_item), ("add part", part_item), ("change status", status), ("create invoice", invoice),
                         ("no duplicate invoice", duplicate_invoice), ("record payment", payment), ("customer status link", public_status),
                         ("customer signature", signature), ("book appointment", booking), ("search customers", search),
                         ("send reply from dashboard", queue_reply), ("logged out redirect", logged_out_redirect),
                         ("tech cannot open finance", tech_blocked_from_finance)]:
        step(name, device, action)
    results.append({"device": device, "flow": "where new vehicle lands", "ok": True, "url": context.get("after_vehicle", "")})

    def menu():
        if device == "phone":
            page.goto(BASE + "/dashboard")
            page.click(".hamburger")
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "phone-menu.png"))
    step("open menu", device, menu)


DEVICE_OPTIONS = {"phone": {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2},
                  "desktop": {"viewport": {"width": 1440, "height": 900}}}

with sync_playwright() as p:
    browser = p.chromium.launch()
    for device, options in [("phone", {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2}),
                            ("desktop", {"viewport": {"width": 1440, "height": 900}})]:
        context = browser.new_context(**options)
        page = context.new_page()
        page.goto(BASE + "/login")
        measure(page, "login", device, 200, [])
        flows(page, device)
        for label, path in PAGES:
            try:
                visit(page, device, label, path)
            except Exception as error:
                results.append({"device": device, "page": label, "status": 0, "errors": [str(error)[:200]]})
        context.close()
    browser.close()
(OUT / "report.json").write_text(json.dumps(results, indent=1))
bad = [r for r in results if r.get("ok") is False or r.get("status", 200) >= 400 or r.get("overflow", 0) > 2]
print(json.dumps(bad, indent=1))
print(f"{len(results)} checks, {len(bad)} failures")
sys.exit(0)
