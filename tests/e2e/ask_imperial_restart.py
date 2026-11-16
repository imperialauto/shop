"""Ask Imperial end to end with a REAL Local Node worker process that is killed and restarted.

Made-up data only. Runs against a local app (started by the workflow) and a stub Ollama
served here on 127.0.0.1; no Quo, no customer contact, nothing leaves the runner.

For each device (desktop, phone), on one synthetic customer who owns two cars with two open ROs:
  1. Office opens RO A, taps Ask Imperial (header / mobile bar), types a labor request.
  2. Worker process #1 claims it; the stub model holds the answer so the worker is mid-task.
  3. Scope changes on RO A (a line is added from the job page) and a synthetic Quo reply
     asks to hold off.
  4. Worker #1 is killed (SIGKILL). Worker #2 starts (same machine, same worker id), reclaims
     after the lease runs out, and reports. The draft comes back "Out of date".
  5. The hold-off message is replayed; the killed worker's late result is submitted.
  6. Checks: right RO, original reading kept, reasons shown, late result refused, stale Apply
     and stale Send refused, exactly one open advisor task.
  7. The advisor revises, enters ALLDATA labor and parts, and applies from the same screen.
  8. Labor unavailable: worker stopped, a request on RO B shows Workstation offline / Needs
     labor lookup with no Apply until labor is entered by hand.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

BASE = "http://127.0.0.1:8099"
OLLAMA_PORT = 11555
OUT = Path("ask-restart-output")
OUT.mkdir(exist_ok=True)
DB = create_engine(os.environ["DATABASE_URL"])
NODE_KEY = os.environ["LOCAL_NODE_WORKER_KEY"]
INTERNAL_KEY = os.environ["INTERNAL_API_KEY"]
PASSWORD = "ask-qa-only-pass"
LEASE = int(os.environ.get("LOCAL_NODE_LEASE_SECONDS", "20"))
READING = {"operations": [{"name": "Brake pads and rotors, front, replace", "system": "brakes", "position": "front"}],
           "vehicle_details_to_confirm": ["Brake package"], "parts_to_price": ["front brake pads", "front rotors"],
           "notes_for_advisor": []}
results = []
HOLD = threading.Event()  # set = the stub model answers; clear = it blocks (worker mid-task)


def log(step, ok=True, **detail):
    entry = {"step": step, "ok": ok, "t": time.strftime("%H:%M:%S"), **detail}
    results.append(entry)
    print(json.dumps(entry), flush=True)
    if not ok:
        raise AssertionError(f"{step}: {detail}")


class StubOllama(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the worker that asked was killed mid-task: that's the point of the test

    def do_GET(self):
        if self.path == "/api/version":
            return self._send({"version": "0.0.0-qa-stub"})
        return self._send({"models": [{"name": "imperial-diagnostics:latest"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        while not HOLD.wait(0.5):  # hold the answer while the worker is "thinking"
            pass
        return self._send({"message": {"content": json.dumps(READING)}})


def sql(query, **params):
    with DB.begin() as conn:
        return conn.execute(text(query), params).mappings().all()


def request_row(request_id):
    return sql("SELECT * FROM assist_requests WHERE id = :i", i=request_id)[0]


def wait_for(what, check, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    log(what, ok=False, timeout=timeout)


class Worker:
    """The real local_node/worker.py, run as its own OS process."""

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.out = open(OUT / f"worker-{name}.log", "w")
        env = {**os.environ, "LOCAL_NODE_WORKER_KEY": NODE_KEY}
        self.proc = subprocess.Popen([sys.executable, "local_node/worker.py", "--config", str(cfg)],
                                     stdout=self.out, stderr=subprocess.STDOUT, env=env)
        RUNNING.append(self)
        log("worker started", worker=name, pid=self.proc.pid)

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        code = self.proc.wait(10)
        log("worker killed", worker=self.name, pid=self.proc.pid, exit_code=code)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)


def write_config(device):
    folder = OUT / f"node-{device}"
    folder.mkdir(exist_ok=True)
    cfg = folder / "config.json"
    cfg.write_text(json.dumps({"backendUrl": BASE, "workerId": "shop-qa-pc", "ollamaUrl": f"http://127.0.0.1:{OLLAMA_PORT}",
                               "pollSeconds": 1, "heartbeatSeconds": 2, "requestTimeoutSeconds": 15,
                               "ollamaTimeoutSeconds": 300, "logDir": str(folder)}))
    return cfg


def login(page, username):
    page.goto(BASE + "/login")
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")


def shot(page, device, name):
    page.screenshot(path=str(OUT / f"{device}-{name}.png"), full_page=True)


RUNNING = []


def run_device(browser, device, options, ro_a, ro_b, phone, conversation):
    context = browser.new_context(**options)
    page = context.new_page()
    login(page, "desk")
    cfg = write_config(device)

    # 1. Normal entry point from inside the job, typed (phone keyboard dictation types the same way)
    page.goto(f"{BASE}/ro/{ro_a}")
    page.click(".ask-top" if device == "phone" else ".ask-desk")
    page.wait_for_url(f"**/ask?ro={ro_a}")
    assert f"RO #{ro_a}" in page.inner_text(".ai-job"), "job not preselected"
    page.fill("#ask-text", "Estimate front brake pads and rotors, customer says grinding")
    page.click("button.ai-go")
    page.wait_for_url("**/ask/requests/*")
    request_id = int(page.url.rsplit("/", 1)[1])
    shot(page, device, "1-queued")
    log("request created from Ask Imperial", device=device, request=request_id, state=page.inner_text("#state-title"))

    # 2. Worker #1 claims; the model is held so it is mid-task
    HOLD.clear()
    first = Worker(f"{device}-1", cfg)
    claimed = wait_for("worker 1 claims", lambda: (r := request_row(request_id))["status"] == "processing" and r)
    old_token = claimed["lease_token"]
    log("worker 1 claimed", device=device, lease_owner=claimed["lease_owner"], attempt=claimed["attempt_count"])
    page.reload()
    shot(page, device, "2-working")

    # 3. Scope change from the job page, then a synthetic Quo reply asking to hold off
    page.goto(f"{BASE}/ro/{ro_a}")
    page.fill("#li-description", "Brake fluid flush")
    page.fill("input[name='unit_price']", "99")
    page.click("text=+ Add Line Item")
    page.wait_for_load_state("networkidle")
    hold_msg = {"conversationId": conversation, "messageId": f"QA-HOLD-{device}", "phone": phone,
                "body": "Please hold off on the brakes until Friday", "knownCustomer": True}
    reply = page.request.post(BASE + "/internal/dispatch/intake", data=json.dumps(hold_msg),
                              headers={"X-Internal-Key": INTERNAL_KEY, "Content-Type": "application/json"})
    log("scope changed and hold-off injected", ok=reply.ok, device=device, intake=reply.status)

    # 4. Real restart: kill worker #1 mid-task, start worker #2, let the lease lapse
    first.kill()
    HOLD.set()
    second = Worker(f"{device}-2", cfg)
    stale = wait_for("worker 2 reclaims and reports", lambda: (r := request_row(request_id))["status"] == "stale" and r,
                     timeout=LEASE + 60)
    log("worker 2 result: draft is stale", device=device, attempts=stale["attempt_count"],
        reasons=json.loads(stale["stale_reason"]))

    # 5. Replay the message; the killed worker's late result arrives
    again = page.request.post(BASE + "/internal/dispatch/intake", data=json.dumps(hold_msg),
                              headers={"X-Internal-Key": INTERNAL_KEY, "Content-Type": "application/json"})
    items = sql("SELECT count(*) AS n FROM dispatch_items WHERE quo_conversation_id = :c", c=conversation)[0]["n"]
    log("hold-off replayed", ok=again.ok and items == 1, device=device, dispatch_items=items)
    late = page.request.post(f"{BASE}/internal/local-node/assist/{request_id}/result",
                             data=json.dumps({"workerId": "shop-qa-pc", "leaseToken": old_token,
                                              "output": json.dumps({**READING, "operations": [
                                                  {"name": "Rear shocks", "system": "", "position": ""}]})}),
                             headers={"X-Local-Node-Key": NODE_KEY, "Content-Type": "application/json"})
    log("late result from killed worker refused", ok=late.status == 409, device=device, status=late.status)

    # 6. What the advisor sees, and what's refused
    row = request_row(request_id)
    events = [e["action"] for e in sql("SELECT action FROM assist_events WHERE request_id = :i ORDER BY id", i=request_id)]
    reasons = json.loads(row["stale_reason"])
    ok = (row["repair_order_id"] == ro_a and "Brake pads and rotors" in row["ai_json"] and "Rear shocks" not in row["ai_json"]
          and any("Brake fluid flush" in r for r in reasons) and any("hold off" in r for r in reasons)
          and "late_result_refused" in events and events.count("claimed") == 1 and "reclaimed" in events)
    log("right RO, original reading kept, reasons recorded", ok=ok, device=device, ro=row["repair_order_id"], events=events)
    page.goto(f"{BASE}/ask/requests/{request_id}")
    body = page.inner_text("body").lower()  # status pills are upper-cased by CSS
    shot(page, device, "3-stale")
    log("advisor sees Out of date with reasons", ok="out of date" in body and "hold off" in body
        and "brake fluid flush" in body
        and page.locator("text=Apply to RO").count() == 0, device=device)
    applied = page.request.post(f"{BASE}/ask/requests/{request_id}/apply", form={"updated": str(row["updated_at"].isoformat())},
                                max_redirects=0)
    send = page.request.post(f"{BASE}/ro/{ro_a}/request-approval", max_redirects=0)
    log("stale Apply and stale Send refused", ok="error=" in (applied.headers.get("location") or "") and send.status == 409,
        device=device, apply_location=applied.headers.get("location"), send_status=send.status)
    open_tasks = sql("SELECT count(*) AS n FROM imperial_tasks WHERE source = 'ask_imperial' AND external_id = :e "
                     "AND status IN ('open','in_progress','blocked')", e=f"assist:{request_id}")[0]["n"]
    all_tasks = sql("SELECT count(*) AS n FROM imperial_tasks WHERE source = 'ask_imperial' AND external_id = :e",
                    e=f"assist:{request_id}")[0]["n"]
    log("exactly one actionable advisor item", ok=open_tasks == 1 and all_tasks == 1, device=device, open=open_tasks)

    # 7. Same screen: revise, enter ALLDATA labor and parts, apply
    page.click("text=Revise draft")
    page.wait_for_load_state("networkidle")
    page.fill("input[name='vehicle_config']", "5.7L RWD, standard brake package")
    page.fill("input[name='guide_hours']", "1.6")
    page.check("input[name='verified']")
    page.fill("input[name='part_price_0']", "89.99")
    page.fill("input[name='part_availability_0']", "In stock")
    page.fill("input[name='shop_manager_ref']", f"SM-QA-{device}")
    page.click("text=Save labor and price it")
    page.wait_for_load_state("networkidle")
    shot(page, device, "4-ready")
    page.click("text=Apply to RO")
    page.wait_for_load_state("networkidle")
    shot(page, device, "5-applied")
    row = request_row(request_id)
    lines = sql("SELECT item_type, quantity, unit_price, approval, description FROM line_items WHERE repair_order_id = :r "
                "ORDER BY id", r=ro_a)
    other = sql("SELECT count(*) AS n FROM line_items WHERE repair_order_id = :r", r=ro_b)[0]["n"]
    ok = (row["status"] == "applied" and any("ALLDATA Repair" in l["description"] for l in lines)
          and all(l["approval"] is None for l in lines) and other == 0)
    log("advisor completed the revised draft from the same screen", ok=ok, device=device,
        lines=[(l["item_type"], float(l["quantity"]), float(l["unit_price"])) for l in lines], other_ro_lines=other)
    done = sql("SELECT status FROM imperial_tasks WHERE source = 'ask_imperial' AND external_id = :e", e=f"assist:{request_id}")
    log("advisor item closed on apply", ok=[d["status"] for d in done] == ["done_verified"], device=device)

    # 8. Labor verification unavailable: worker stopped, request on the other car
    second.kill()
    wait_for("workstation shows offline", lambda: page.request.get(f"{BASE}/ask/requests/{request_id}/state").json()
             .get("online") is False, timeout=int(os.environ.get("LOCAL_NODE_OFFLINE_SECONDS", "10")) + 30)
    page.goto(f"{BASE}/ask?ro={ro_b}")
    page.fill("#ask-text", "Quote a clutch job on this car")
    page.click("button.ai-go")
    page.wait_for_url("**/ask/requests/*")
    offline_id = int(page.url.rsplit("/", 1)[1])
    text_now = page.inner_text("body").lower()
    shot(page, device, "6-offline")
    log("labor unavailable: offline and Needs labor lookup, no Apply", ok="workstation offline" in text_now
        and "needs labor lookup" in text_now and page.locator("text=Apply to RO").count() == 0, device=device)
    page.fill("input[name='vehicle_config']", "2.0L, 6-speed dual clutch")
    page.fill("input[name='operation']", "Clutch assembly, replace")
    page.fill("input[name='guide_hours']", "5.2")
    page.check("input[name='verified']")
    page.click("text=Save labor and price it")
    page.wait_for_load_state("networkidle")
    page.click("text=Apply to RO")
    page.wait_for_load_state("networkidle")
    log("labor entered by hand and applied while offline", ok=request_row(offline_id)["status"] == "applied", device=device)
    context.close()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", OLLAMA_PORT), StubOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    devices = [("desktop", {"viewport": {"width": 1440, "height": 900}}, 11, 12, "+16025550411", "CNqa-desktop"),
               ("phone", {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True,
                          "device_scale_factor": 2}, 13, 14, "+16025550413", "CNqa-phone")]
    failed = False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for device, options, ro_a, ro_b, phone, conversation in devices:
            try:
                run_device(browser, device, options, ro_a, ro_b, phone, conversation)
            except Exception as error:  # record and continue with the other device
                failed = True
                entry = {"step": "device failed", "ok": False, "device": device, "error": str(error)[:800]}
                results.append(entry)
                print(json.dumps(entry), flush=True)
            finally:
                for worker in RUNNING:  # a device's workers never carry over to the next device
                    worker.stop()
                RUNNING.clear()
        browser.close()
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str))
    bad = [r for r in results if not r.get("ok")]
    print(f"{len(results) - len(bad)} passed, {len(bad)} failed")
    sys.exit(1 if failed or bad else 0)


if __name__ == "__main__":
    main()
