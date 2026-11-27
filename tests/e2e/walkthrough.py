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
         ("calendar", "/appointments/calendar"), ("settings", "/settings"), ("business-settings", "/settings/business"), ("alert-center", "/alerts"), ("my-day", "/my-day"), ("live-control", "/live/control"),
         ("site-home", "/site/"), ("site-services", "/site/services"), ("site-diesel", "/site/services/diesel"),
         ("site-heavy-duty", "/site/services/heavy-duty"), ("site-contact", "/site/contact"), ("site-book", "/site/book"),
         ("site-book-times", "/site/book?service=diagnostic"), ("site-estimate", "/site/estimate"),
         ("site-mobile", "/site/book?service=battery-charging&where=mobile&zip=85000"), ("site-pricing", "/site/pricing"),
         ("site-roadside-service", "/site/services/mobile-roadside"), ("security-privacy", "/settings/security-privacy"), ("site-faq", "/site/faq"), ("site-status", "/site/status"),
         ("site-quote", "/site/quote"), ("instant-price-book", "/settings/instant-quotes")]


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

    def master_tasks():
        title = "Synthetic SMS task for " + device + " " + ("long title " * 12).strip()
        captured = page.request.post(
            BASE + "/internal/imperial/tasks/capture",
            headers={"X-Internal-Key": "walkthrough-task-fixture-key"},
            data={"messageId": f"walkthrough-{device}", "title": title,
                  "dueAt": "2027-01-08T11:00:00-07:00"},
        )
        assert captured.ok, captured.text()
        task_id = captured.json()["id"]
        page.goto(BASE + "/dashboard")
        page.get_by_role("link", name="Master Tasks", exact=True).click()
        page.wait_for_url("**/dashboard/tasks")
        page.goto(BASE + "/dashboard/tasks?tab=later&scope=review")  # SMS capture stays in Unsorted
        task = page.locator("article").filter(has_text=title)
        assert task.count() == 1
        assert "11:00 AM" in task.inner_text()
        task.get_by_text("Pick a time", exact=True).click()
        task.get_by_label("Remind me (Phoenix time)").fill("2027-01-08T14:00")
        task.get_by_role("button", name="Save").click()
        assert "2:00 PM" in task.inner_text()
        measure(page, "master-tasks", device, 200, [])
        task.get_by_role("button", name="✓ Done").click()
        assert task.count() == 0
        page.goto(BASE + "/dashboard/tasks?tab=done&scope=review")
        assert task.is_visible()
        task.get_by_role("button", name="Reopen").click()
        page.goto(BASE + "/dashboard/tasks?tab=later&scope=review")
        assert task.is_visible()

        created_title = "Dashboard task for " + device
        page.get_by_label("New task", exact=True).fill(created_title)
        page.get_by_role("form", name="Add a shop task").get_by_label("RO number", exact=True).fill("1")
        page.get_by_role("button", name="Add", exact=True).click()
        assert page.locator("article").filter(has_text=created_title).is_visible()
        stored = page.request.get(
            BASE + f"/internal/imperial/tasks/{task_id}",
            headers={"X-Internal-Key": "walkthrough-task-fixture-key"},
        )
        assert stored.ok, stored.text()
        assert stored.json()["status"] == "open"
        assert stored.json()["dueAt"] == "2027-01-08T21:00:00+00:00"

        page.get_by_role("link", name="My To-do", exact=True).click()
        assert not page.get_by_text(created_title, exact=True).count()
        todo_title = f"Call the fixture vendor on {device}"
        page.get_by_label("New to-do").fill(todo_title)
        page.get_by_role("form", name="Add a personal to-do").get_by_role("button", name="Add", exact=True).click()
        todo = page.locator("article").filter(has_text=todo_title)
        assert todo.is_visible()
        todo.get_by_role("button", name="Backburner").click()
        assert not todo.is_visible()
        page.get_by_text("Backburner (", exact=False).click()
        todo.get_by_role("button", name="Do now").click()
        assert todo.is_visible()

    def customer():
        page.goto(BASE + "/customers/new")
        page.fill("[name=first_name]", "Flow" + device)
        page.fill("[name=last_name]", "Check")
        page.fill("[name=phone]", "60255501" + str(10 + len(device)))  # one number per device, so no duplicate match
        page.click("form button[value=customer]")
        page.wait_for_load_state("networkidle")
        context["customer"] = int(re.search(r"/customers/(\d+)", page.url).group(1))

    def vehicle():
        page.goto(f"{BASE}/vehicles/new?customer_id={context['customer']}")
        page.select_option("[name=customer_id]", str(context["customer"]))
        page.select_option("[name=year]", "2018")
        # Signed-in vehicle forms use the year -> make -> model dropdowns;
        # "Other (type it)" works without the NHTSA model list (offline CI).
        page.select_option("select[aria-label=Make]", "Honda")
        page.select_option("select[aria-label=Model]", "__other__")
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


    def edit_line_item():
        page.get_by_role("link", name="Edit Diagnostic", exact=True).click()
        page.wait_for_url(re.compile(r"/ro/\d+/edit-line-item/\d+$"))
        assert page.input_value("#edit-description") == "Diagnostic"
        measure(page, "edit-line-item", device, 200, [])
        page.fill("#edit-description", "Diagnostic inspection")
        page.fill("#edit-quantity", "0.5")
        page.fill("#edit-price", "300")
        page.get_by_role("button", name="Save changes", exact=True).click()
        page.wait_for_url(re.compile(r"/ro/\d+#line-items$"))
        assert "Diagnostic inspection" in page.inner_text("body")
        assert "$150.00" in page.inner_text("body")
        page.get_by_role("link", name="Edit Diagnostic inspection", exact=True).click()
        assert page.input_value("#edit-quantity") == "0.5"
        assert page.input_value("#edit-price") == "300.0"
        page.get_by_role("link", name="Cancel", exact=True).click()
        page.wait_for_url(re.compile(r"/ro/\d+#line-items$"))

    def part_item():
        page.fill("#li-description", "Oil filter")
        page.select_option("[name=item_type]", "part")
        page.fill("[name=quantity]", "1")
        page.fill("[name=unit_price]", "100")
        page.click(f"form[action='/ro/{context['ro']}/add-line-item'] button[type=submit]")
        page.wait_for_load_state("networkidle")

    def status():
        # Customer approval must already have been recorded by the signature
        # flow. Work starts before it can be marked complete; do not jump
        # directly from intake/approval_received to complete.
        for next_status in ("in_progress", "complete"):
            with page.expect_navigation():
                page.select_option("select[name=status]", next_status)
            page.wait_for_load_state("networkidle")
            actual = page.input_value("select[name=status]")
            assert actual == next_status, (
                f"expected {next_status}, got {actual!r}; url={page.url}; "
                f"available={page.locator('select[name=status] option').evaluate_all('(opts) => opts.map(o => o.value)')}"
            )

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
        assert page.locator("nav.status-navigation a[href='/site/']").count() == 1, "no home link from status"
        assert page.locator("nav.status-navigation a[href='/site/status']").count() == 1, "no other-status lookup link"
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
        tech.wait_for_url("**/my-day")
        assert tech.locator("a[href='/finance/']").count() == 0
        assert tech.goto(BASE + "/finance/").status == 403
        assert tech.goto(BASE + "/invoices/").status == 403
        assert tech.goto(BASE + "/customers/").status == 403
        assert tech.goto(BASE + "/ro/1").status == 200
        assert tech.goto(BASE + "/appointments/calendar").status == 200
        tech.context.close()

    def sign_in(name, password="walkthrough-only-pass"):
        staff = page.context.browser.new_context().new_page()
        staff.goto(BASE + "/login")
        staff.fill("[name=username]", name)
        staff.fill("[name=password]", password)
        staff.click("button[type=submit]")
        staff.wait_for_load_state()
        return staff

    def admin_limits():
        admin = sign_in("manager")
        assert admin.goto(BASE + "/invoices/").status == 200
        assert admin.goto(BASE + "/finance/").status == 403
        assert "Add a Team Member" not in admin.goto(BASE + "/settings").text()
        admin.context.close()

    def office_limits():
        office = sign_in("office")
        assert office.goto(BASE + "/dashboard").status == 200
        assert office.goto(BASE + "/customers/").status == 200
        assert office.goto(BASE + "/finance/").status == 403
        assert office.goto(BASE + "/live/control").status == 403
        office.context.close()

    def new_login_must_change_password():
        name = "newhire" + device[0]
        temporary, chosen = "temporary-pass-only", "my-own-new-password"
        page.goto(BASE + "/settings")
        page.fill("form[action='/settings/users/create'] [name=full_name]", "New Hire")
        page.fill("form[action='/settings/users/create'] [name=username]", name)
        page.fill("form[action='/settings/users/create'] [name=password]", temporary)
        page.click("form[action='/settings/users/create'] button[type=submit]")
        assert "Login created" in page.inner_text("body")
        hire = sign_in(name, temporary)
        assert "/settings" in hire.url, hire.url
        hire.goto(BASE + "/ro/")
        assert "/settings" in hire.url, hire.url
        hire.fill("[name=current_password]", temporary)
        hire.fill("[name=new_password]", chosen)
        hire.click("form[action='/settings/change-password'] button[type=submit]")
        assert "Password updated" in hire.inner_text("body")
        assert hire.goto(BASE + "/ro/").status == 200
        hire.context.close()
        row = page.locator("tr", has_text=name)
        page.goto(BASE + "/settings")
        row.locator("button", has_text="Turn off").click()
        assert "turned off" in page.inner_text("body")
        off = sign_in(name, chosen)
        assert "/login" in off.url, off.url
        off.context.close()

    def search():
        page.goto(BASE + "/customers/?search=Testcase")
        assert "Lee" in page.inner_text("body") and "Pat Sample" not in page.inner_text("body")

    def queue_reply():
        page.goto(BASE + "/dashboard?view=urgent")
        before = page.locator("button[value=approve]").count()
        name = "Sample Contact 1" if device == "phone" else "Sample Contact 2"
        if device == "desktop":
            # Home shows only the top few; the full queue lives on the Needs you page.
            page.goto(BASE + "/dashboard/inbox")
            before = page.locator("button[value=approve]").count()
        page.locator("details summary", has_text=name).first.click()
        page.locator("details[open] textarea[name=reply_text]").first.fill("Edited test reply")
        page.locator("details[open] button[value=approve]").first.click()
        page.wait_for_load_state("networkidle")
        page.goto(BASE + ("/dashboard?view=urgent" if device == "phone" else "/dashboard/inbox"))
        assert page.locator("button[value=approve]").count() < before, "item still waiting after sending"

    def signature():
        page.goto(f"{BASE}/ro/{context['ro']}")
        # Status-first approvals: the customer can only approve lines the shop has sent for approval.
        # This marks them waiting on the customer; any approval text is only queued for the owner's YES.
        ask = page.locator(f"form[action='/ro/{context['ro']}/request-approval'] button[type=submit]")
        if ask.count():
            with page.expect_navigation():
                ask.first.click()
            page.wait_for_load_state("networkidle")
        link = page.locator("a[href*='/sign/ro/']")
        assert link.count(), "no signature link"
        href = link.first.get_attribute("href")
        page.goto(href if href.startswith("http") else BASE + href)
        choices = page.locator("input[type=radio][value=approved]")
        assert choices.count(), "no lines waiting for the customer's decision"
        for i in range(choices.count()):
            choices.nth(i).check()
        page.fill("[name=approved_by]", "Flow Check")
        box = page.locator("#sig-canvas").bounding_box()
        page.mouse.move(box["x"] + 20, box["y"] + 40)
        page.mouse.down()
        page.mouse.move(box["x"] + 150, box["y"] + 90, steps=6)
        page.mouse.up()
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        measure(page, "signed", device, 200, [])
        assert "estimate response received" in page.inner_text("body").lower(), "no confirmation after signing"
        page.goto(f"{BASE}/ro/{context['ro']}")
        assert "Approved" in page.inner_text("body"), "customer's approval not shown on the job"

    def booking():
        day = "2027-01-05" if device == "phone" else "2027-01-06"
        page.goto(f"{BASE}/ro/{context['ro']}")
        page.click("a[href*='/appointments/calendar?ro=']")
        page.wait_for_load_state("networkidle")
        assert page.input_value("[name=repair_order_id]") == str(context["ro"])
        page.fill("[name=starts_at]", f"{day}T11:00")
        page.fill("[name=ends_at]", f"{day}T12:00")
        page.click("#appointment-form button[type=submit]")
        page.locator("#booking-message", has_text="saved").wait_for()
        page.locator("#calendar-rows tr", has_text=f"#{context['ro']}").first.wait_for(timeout=8000)
        page.fill("[name=starts_at]", f"{day}T12:30")
        page.fill("[name=ends_at]", f"{day}T13:30")
        page.click("#appointment-form button[type=submit]")
        page.locator("#booking-message", has_text="already").wait_for()
        page.screenshot(path=str(OUT / f"{device}-booked.png"), full_page=True)

    def site_booking():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/")
        visitor.click(".hero a[href='/site/book']")
        visitor.click("a[href='/site/book?service=diagnostic']")
        more = visitor.locator("#booking-show-later-dates")
        assert more.is_visible(), "long booking list should start compact"
        assert visitor.locator("#booking-time-choices details.day[hidden]").count() >= 1, "later dates not collapsed"
        more.click()
        assert visitor.locator("#booking-time-choices details.day[hidden]").count() == 0, "later dates not revealed"
        assert more.is_hidden(), "later-dates action should disappear after expanding"
        # Progressive hiding must not prevent booking without JavaScript.
        no_js_context = visitor.context.browser.new_context(**{**page_options, "java_script_enabled": False})
        try:
            no_js = no_js_context.new_page()
            no_js.goto(BASE + "/site/book?service=diagnostic")
            assert no_js.locator("#booking-time-choices details.day[hidden]").count() == 0, "no-JS dates hidden"
            assert no_js.locator("details.day").count() > 5, "no-JS availability incomplete"
        finally:
            no_js_context.close()
        visitor.locator("label.choice", has=visitor.locator("input[name=slot_id]")).first.click()
        assert visitor.locator("#booking-time-choices").is_hidden(), "times stayed expanded"
        assert visitor.locator("#booking-time-summary").is_visible(), "no selected-time summary"
        assert visitor.locator("#booking-next-step").is_visible(), "customer details did not advance"
        visitor.locator("#booking-change-time").click()
        assert visitor.locator("#booking-time-choices").is_visible(), "cannot change time"
        assert visitor.locator("#booking-next-step").is_hidden(), "details visible while changing time"
        visitor.locator("label.choice", has=visitor.locator("input[name=slot_id]:checked")).first.click()
        assert visitor.locator("#booking-next-step").is_visible(), "reselecting the same time did not advance"
        visitor.fill("[name=year]", "2016")
        visitor.fill("[name=make]", "Ford")
        visitor.fill("[name=model]", "Focus")
        visitor.fill("[name=first_name]", "Web" + device)
        visitor.fill("[name=last_name]", "Visitor")
        visitor.check("input[name=contact_method][value=text]")
        visitor.fill("[name=phone]", "6025550123")
        visitor.fill("[name=notes]", "Made-up walkthrough booking")
        visitor.click("form[action='/site/book'] button[type=submit]")
        visitor.wait_for_url("**/site/book/received")
        body = visitor.inner_text("body")
        assert "WEB-" in body and "$10" in body and "confirmed" in body.lower(), body[:300]
        assert "Add to my calendar" in body, body[:300]
        measure(visitor, "site-booked", device, 200, [])
        reference = visitor.inner_text("#ref").strip()
        visitor.reload()  # a refresh keeps the reference on screen
        assert reference in visitor.inner_text("#ref")
        visitor.click("a:has-text('Open my status page')")
        assert "/status/" in visitor.url
        status_body = visitor.inner_text("body")
        assert reference in status_body and "Arizona time" in status_body, status_body[:300]  # labels are styled in capitals
        assert "Confirm before contacting" not in status_body
        measure(visitor, "site-status-result", device, 200, [])
        # The booking and an authorized status-link visit are remembered only in
        # this signed browser session; the unrelated fresh browser sees nothing.
        visitor.goto(BASE + "/site/status")
        assert visitor.locator("a:has-text('Open my saved status')").count() == 1
        assert reference in visitor.locator("[aria-label='Your recently opened vehicle status']").inner_text()
        visitor.click("a:has-text('Open my saved status')")
        assert "/status/" in visitor.url
        fresh = page.context.browser.new_context(**page_options).new_page()
        fresh.goto(BASE + "/site/status")
        assert fresh.locator("a:has-text('Open my saved status')").count() == 0
        fresh.goto(visitor.url)
        assert fresh.locator("a:has-text('Look up another vehicle')").count() == 1
        fresh.click("a:has-text('Look up another vehicle')")
        assert fresh.locator("a:has-text('Open my saved status')").count() == 1
        fresh.context.close()
        visitor.goto(BASE + "/site/status")
        visitor.fill("[name=reference]", reference)
        visitor.fill("[name=phone]", "6025550123")
        visitor.click("form[action='/site/status'] button[type=submit]")
        visitor.wait_for_url("**/status/*")
        visitor.goto(BASE + "/site/status")
        visitor.fill("[name=reference]", reference)
        visitor.fill("[name=phone]", "6025550100")
        visitor.click("form[action='/site/status'] button[type=submit]")
        assert "could not match" in visitor.inner_text("body")
        visitor.fill("#lost-phone", "6025550123")  # phone only since PR 130; same reply either way
        visitor.click("form[action='/site/status/lost'] button[type=submit]")
        assert "If that matches a booking or request" in visitor.inner_text("body")
        measure(visitor, "site-lost-reference", device, 200, [])
        visitor.context.close()
        page.goto(BASE + "/appointments/")
        assert "confirmed" in page.inner_text("body"), "online booking not on the staff list"

    def site_mobile_booking():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/book?service=battery-charging")
        visitor.locator("label.choice", has=visitor.locator("input[value=mobile]")).click()
        visitor.fill("[name=zip]", "85000")
        visitor.click("button:has-text('Update times')")
        assert "$5.00" in visitor.inner_text("body") and "$45.00" in visitor.inner_text("body")
        visitor.locator("details[open] label.choice", has=visitor.locator("input[name=slot_id]")).last.click()
        visitor.fill("[name=street]", "100 Test St")
        visitor.fill("[name=year]", "2016")
        visitor.fill("[name=make]", "Ford")
        visitor.fill("[name=model]", "Focus")
        visitor.fill("[name=first_name]", "Mobile" + device)
        visitor.fill("[name=last_name]", "Visitor")
        visitor.check("input[name=contact_method][value=text]")
        visitor.fill("[name=phone]", "6025550125")
        visitor.click("button:has-text('Book this time')")
        visitor.wait_for_url("**/site/book/received")
        assert "mobile fee" in visitor.inner_text("body")
        measure(visitor, "site-mobile-booked", device, 200, [])
        visitor.context.close()

    def site_taken_time():
        one = page.context.browser.new_context(**page_options).new_page()
        one.goto(BASE + "/site/book?service=pre-purchase")
        choice = one.locator("input[name=slot_id]").first
        slot = choice.get_attribute("value")
        form = {"service": "pre-purchase", "slot_id": slot, "first_name": "Clash", "last_name": "Visitor", "phone": "6025550126",
                "contact_method": "text", "make": "Ford", "model": "Focus", "where": "shop", "zip": ""}
        first = one.request.post(BASE + "/site/book", form=form, max_redirects=0)
        assert first.status == 303, first.status
        second = one.request.post(BASE + "/site/book", form={**form, "phone": "6025550127"}, max_redirects=0)
        assert second.status == 409, second.status
        one.goto(BASE + "/site/book?service=pre-purchase")
        assert one.locator(f"input[name=slot_id][value='{slot}']").count() == 0
        one.context.close()

    def site_estimate():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/services/diesel")
        visitor.click("a[href='/site/estimate?service=diesel']")
        assert visitor.input_value("[name=service_line]") == "diesel"
        visitor.fill("textarea[name=description]", "Made-up walkthrough: hard start when cold")
        visitor.fill("[name=vin]", "1FTFW1ET5DFC10312")
        visitor.fill("[name=first_name]", "Est" + device)
        visitor.fill("[name=last_name]", "Visitor")
        visitor.check("input[name=contact_method][value=text]")
        visitor.fill("[name=phone]", "6025550124")
        visitor.click("form[action='/site/estimate'] button[type=submit]")
        visitor.wait_for_url("**/site/estimate/received")
        measure(visitor, "site-estimate-received", device, 200, [])
        visitor.context.close()

    def site_brake_package():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/book")
        visitor.click("a[href='/site/book?service=brake-pads-rotors']")
        body = visitor.inner_text("body")
        assert "From $300 per axle" in body and "Final price confirmed before work" in body, body[:400]
        # The progressive form reveals vehicle package choices after selecting a time.
        visitor.locator("details[open] label.choice", has=visitor.locator("input[name=slot_id]")).last.click()
        assert visitor.locator("#booking-next-step").is_visible(), "package step did not advance"
        visitor.locator("label.choice", has=visitor.locator("input[value=pickup]")).click()
        visitor.select_option("#axles", "2")
        visitor.fill("[name=year]", "2019")
        visitor.fill("[name=make]", "Ram")
        visitor.fill("[name=model]", "1500")
        visitor.fill("[name=first_name]", "Brake" + device)
        visitor.fill("[name=last_name]", "Visitor")
        visitor.check("input[name=contact_method][value=text]")
        visitor.fill("[name=phone]", "6025550131" if device == "phone" else "6025550132")
        measure(visitor, "site-brake-package", device, 200, [])
        visitor.click("button:has-text('Book this time')")
        visitor.wait_for_url("**/site/book/received")
        body = visitor.inner_text("body")
        assert "From $800 for 2 axles" in body and "Final price is confirmed" in body, body[:400]
        measure(visitor, "site-brake-booked", device, 200, [])
        visitor.context.close()

    def site_request_unpriced():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/book")
        assert "call or text" not in visitor.inner_text("main").lower()
        visitor.click("a[href='/site/estimate?about=ac-check']")
        assert "A/C performance check" in visitor.input_value("textarea[name=description]")
        measure(visitor, "site-request-unpriced", device, 200, [])
        visitor.context.close()

    def site_roadside():
        visitor = page.context.browser.new_context(**page_options).new_page()
        visitor.goto(BASE + "/site/services/towing")
        visitor.click("a.btn-gold[href='/site/roadside']")
        visitor.locator("label.choice", has=visitor.locator("input[value=tow]")).click()
        visitor.fill("#location", "Made-up: 59th Ave and Northern")
        visitor.fill("[name=make]", "Ford")
        visitor.fill("[name=model]", "Focus")
        visitor.fill("[name=first_name]", "Road" + device)
        visitor.fill("[name=last_name]", "Visitor")
        visitor.check("input[name=contact_method][value=text]")
        visitor.fill("[name=phone]", "6025550141" if device == "phone" else "6025550142")
        measure(visitor, "site-roadside", device, 200, [])
        visitor.click("form[action='/site/roadside'] button[type=submit]")
        visitor.wait_for_url("**/site/estimate/received")
        assert "Nothing is dispatched until we call you" in visitor.inner_text("body")
        measure(visitor, "site-roadside-received", device, 200, [])
        visitor.context.close()


    def quote_visitor():
        return page.context.browser.new_context(**page_options).new_page()

    def pick_job(visitor, job, year="2016", make="Ford", model="Focus", where="shop", zip_code=""):
        visitor.goto(BASE + "/site/quote")
        visitor.select_option("#job", job)
        visitor.locator("label.choice", has=visitor.locator(f"input[name=where][value={where}]")).click()
        visitor.fill("#zip", zip_code)
        visitor.fill("#year", year)
        visitor.fill("#make", make)
        visitor.fill("#model", model)
        visitor.click("form[action='/site/quote'] button[type=submit]")
        visitor.wait_for_load_state("networkidle")

    def site_instant_price():
        visitor = quote_visitor()
        visitor.goto(BASE + "/site/")
        visitor.click(".hero a[href='/site/quote']")
        assert visitor.url.endswith("/site/quote"), visitor.url
        measure(visitor, "site-quote-form", device, 200, [])
        pick_job(visitor, "oil-change")
        price = visitor.inner_text("#price")
        assert "$86.24" in price and "Tax (parts only)" in price, price[:400]
        measure(visitor, "site-quote-price", device, 200, [])
        visitor.fill("#first_name", "Quote" + device)
        visitor.fill("#last_name", "Visitor")
        visitor.fill("#phone", "6025550151" if device == "phone" else "6025550152")
        visitor.fill("#when", "Saturday morning")
        visitor.check("input[name=contact_method][value=text]")
        visitor.click("form[action='/site/quote/hold'] button[type=submit]")
        visitor.wait_for_url("**/site/estimate/received")
        body = visitor.inner_text("body")
        assert "Price held: $86.24" in body, body[:400]
        measure(visitor, "site-quote-held", device, 200, [])
        reference = visitor.inner_text("#ref").strip()
        assert reference.startswith("WEB-"), reference
        context["quote_ro"] = int(reference.split("-", 1)[1])
        pick_job(visitor, "oil-change")  # holding the same price again must not make a second job
        visitor.fill("#first_name", "Quote" + device)
        visitor.fill("#last_name", "Visitor")
        visitor.fill("#phone", "6025550151" if device == "phone" else "6025550152")
        visitor.check("input[name=contact_method][value=text]")
        visitor.click("form[action='/site/quote/hold'] button[type=submit]")
        visitor.wait_for_url("**/site/estimate/received")
        assert visitor.inner_text("#ref").strip() == reference, "held the same price twice"
        visitor.context.close()

    def instant_price_on_dashboard():
        ro = context["quote_ro"]
        page.goto(f"{BASE}/ro/{ro}")
        body = page.inner_text("body")
        assert "Instant price from the website" in body and "Saturday morning" in body, body[:600]
        assert "$86.24" in body, "job total does not match the price the customer saw"
        measure(page, "instant-price-job", device, 200, [])
        page.goto(BASE + "/dashboard/inbox")
        assert "Instant price $86.24" in page.inner_text("body"), "no owner queue item for the held price"

    def site_instant_price_problems():
        visitor = quote_visitor()
        pick_job(visitor, "oil-change", year="19")
        assert visitor.locator("[role=alert]").count() == 1, "bad year accepted"
        assert visitor.input_value("#make") == "Ford", "form lost what the customer typed"
        pick_job(visitor, "oil-change", where="onsite", zip_code="85000")
        alert = visitor.locator("[role=alert]")
        assert alert.count() == 1 and alert.locator("a[href='/site/estimate']").count() == 1, "shop-only job priced on site"
        measure(visitor, "site-quote-no-price", device, 200, [])
        visitor.goto(BASE + "/site/estimate")
        visitor.click("a[href='/site/quote']")
        assert visitor.url.endswith("/site/quote"), visitor.url
        visitor.context.close()

    def instant_price_book():
        page.goto(BASE + "/settings")
        page.click("a[href='/settings/instant-quotes']")
        page.wait_for_url("**/settings/instant-quotes")
        card = page.locator("section.card", has_text="Add a job")
        card.locator("[name^=name_]").fill("Battery install (walkthrough)")
        card.locator("[name^=hours_]").fill("0.5")
        card.locator("[name^=parts_]").fill("150")
        card.locator("[name^=supplies_]").check()
        card.locator("[name^=mobile_]").check()
        page.click("button:has-text('Save price book')")
        page.wait_for_load_state("networkidle")
        assert "Saved." in page.inner_text("body"), page.inner_text("body")[:400]
        measure(page, "instant-price-book-saved", device, 200, [])
        bad = page.locator("section.card", has_text="Add a job")
        bad.locator("[name^=name_]").fill("Broken job")
        bad.locator("[name^=hours_]").fill("lots")
        page.click("button:has-text('Save price book')")
        assert "Nothing was saved" in page.inner_text("body"), "bad hours were saved"
        visitor = quote_visitor()
        visitor.goto(BASE + "/site/quote")
        key = visitor.eval_on_selector("#job", "s => [...s.options].find(o => o.text.startsWith('Battery install')).value")
        assert "Broken job" not in visitor.inner_text("#job")
        pick_job(visitor, key)
        assert "$246.30" in visitor.inner_text("#price"), visitor.inner_text("#price")[:400]
        pick_job(visitor, key, where="onsite", zip_code="85000")
        price = visitor.inner_text("#price")
        assert "$334.55" in price and "Mobile service call" in price, price[:400]
        pick_job(visitor, key, where="onsite", zip_code="99999")
        assert visitor.locator("[role=alert] a[href='/site/estimate']").count() == 1, "unpriced ZIP got a price"
        visitor.context.close()
        page.goto(BASE + "/settings/instant-quotes")
        page.once("dialog", lambda dialog: dialog.accept())
        page.click("button:has-text('Back to defaults')")
        page.wait_for_load_state("networkidle")
        assert "Battery install (walkthrough)" not in page.content(), "Back to defaults kept the added job"

    for name, action in [("wrong password", wrong_password), ("login", login), ("Master Tasks create, reschedule, complete and reopen", master_tasks), ("public site booking", site_booking), ("public mobile booking", site_mobile_booking), ("public time conflict", site_taken_time), ("public site estimate", site_estimate),
                         ("public brake package", site_brake_package), ("public unpriced request", site_request_unpriced), ("public roadside", site_roadside), ("instant price and hold", site_instant_price), ("held price on the dashboard", instant_price_on_dashboard), ("instant price errors", site_instant_price_problems), ("instant price book", instant_price_book), ("new customer", customer), ("new vehicle", vehicle), ("new repair order", repair_order),
                         ("add line item", line_item), ("edit line item", edit_line_item), ("add part", part_item), ("customer status link", public_status),
                         ("customer signature", signature), ("change status", status), ("create invoice", invoice),
                         ("no duplicate invoice", duplicate_invoice), ("record payment", payment),
                         ("book appointment", booking), ("search customers", search),
                         ("send reply from dashboard", queue_reply), ("logged out redirect", logged_out_redirect),
                         ("tech limits", tech_blocked_from_finance),
                         ("admin limits", admin_limits), ("office limits", office_limits), ("new login", new_login_must_change_password)]:
        step(name, device, action)
    results.append({"device": device, "flow": "where new vehicle lands", "ok": True, "url": context.get("after_vehicle", "")})

    def menu():
        if device == "phone":
            page.goto(BASE + "/dashboard")
            page.click(".hamburger")
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "phone-menu.png"))
    step("open menu", device, menu)

    def report_keeps_form():
        # Fix list #130: a report sent from a half-filled form must not wipe it.
        page.goto(BASE + "/customers/new")
        page.fill("input[name='first_name']", "Halfway")
        page.click("#problem-fab")
        page.fill("#problem-dialog textarea", "Walkthrough: report from the new customer form")
        page.click("#problem-dialog button:not([type='button'])")
        page.wait_for_selector("text=Saved to the Fix list as #")
        assert page.url.endswith("/customers/new"), page.url
        assert page.input_value("input[name='first_name']") == "Halfway"
    step("report keeps the form", device, report_keeps_form)


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
# A nonexistent repair order is deliberately probed on each device. A 404 is
# the expected security/error-handling result, not a failed acceptance check.
# Fail the walkthrough if that page unexpectedly succeeds or gives a 5xx.
unexpected = []
expected_not_found = []
for result in results:
    if result.get("page") == "missing-page":
        if result.get("status") == 404:
            expected_not_found.append(result)
        else:
            unexpected.append(result)
    elif result.get("ok") is False or result.get("status", 200) >= 400 or result.get("overflow", 0) > 2:
        unexpected.append(result)
print(json.dumps(unexpected, indent=1))
print(f"{len(results)} checks, {len(unexpected)} failures, {len(expected_not_found)} expected 404 probes")
sys.exit(1 if unexpected else 0)
