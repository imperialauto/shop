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
         ("finance", "/finance/"), ("customers", "/customers/"), ("fleet", "/customers/?type_filter=fleet"), ("customer-new", "/customers/new"),
         ("customer-detail", "/customers/1"), ("vehicle-new", "/vehicles/new?customer_id=1"), ("vehicle-detail", "/vehicles/1"),
         ("calendar", "/appointments/calendar"), ("settings", "/settings"), ("live-control", "/live/control")]


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
        assert "$8.60" in text and "$258.60" in text, "tax should be 8.6% of the $100 part only"

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

    for name, action in [("login", login), ("new customer", customer), ("new vehicle", vehicle), ("new repair order", repair_order),
                         ("add line item", line_item), ("add part", part_item), ("change status", status), ("create invoice", invoice),
                         ("no duplicate invoice", duplicate_invoice), ("record payment", payment), ("customer status link", public_status)]:
        step(name, device, action)
    results.append({"device": device, "flow": "where new vehicle lands", "ok": True, "url": context.get("after_vehicle", "")})

    def menu():
        if device == "phone":
            page.goto(BASE + "/dashboard")
            page.click(".hamburger")
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "phone-menu.png"))
    step("open menu", device, menu)


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
