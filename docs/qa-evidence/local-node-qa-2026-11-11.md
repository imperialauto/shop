# Imperial Local Node — QA evidence, 2026-11-11 (synthetic data only)

Captured from `local-node-qa` (`imperial-pr52-preview / preview`, database `Postgres-ltXm`) before teardown.
All customers, vehicles and jobs are synthetic preview seeds (`Made-up preview job N`). Times are UTC on 2026-11-12.
Workstation: `shop-jaes-desktop` (Windows 11, PowerShell 5.1.26100, Python 3.12.10, Ollama 0.40.16, RTX 3060 12 GB, model `imperial-diagnostics:latest` on `qwen3:8b-q4_K_M`).

## Reviews and full audit history

| Review | Job | Final state | Prompt | Codes supplied | Tech notes sent to the AI |
|---|---|---|---|---|---|
| #3 | 2 | Approved (Preview Owner) | diag-v1 | P0301 — not supplied | none |
| #4 | 3 | Approved (QA Tech) | diag-v1 | P0301 — active | coil 1↔3 swap, misfire **followed** to cyl 3 |
| #5 | 4 | Draft | diag-v2 | P0301 — active | coil swap, **followed** |
| #6 | 2 | Cancelled | diag-v2 | P0301 — active | *earlier approved review #3 text* + coil swap, **stayed** (the bug fixed in `dd2ee6e`) |
| #7 | 2 | Draft | diag-v2 | P0301 — active | coil swap, **stayed** (approved block correctly excluded) |
| #8 | 4 | Draft | diag-v2 | P0301 — active | coil swap, **followed** |
| #9 | 4 | Draft | diag-v2 | P0301 — active | coil swap, **followed** |
| #10 | 2 | Draft | diag-v2 | P0301 — active | coil swap, **stayed** |
| #11 | 3 | Draft | diag-v2 | P0301 — active | coil swap, **followed** (queued while the worker was offline) |

```
#3  00:32:36 Preview Owner requested {"codes":[{"code":"P0301","status":"not supplied"}]}
    00:51:27 node claimed attempt 1/3 · 00:51:32 node draft_ready 4843 ms
    00:52:33 Preview Owner approved {"edited_by_technician":true,"saved_to_job":true}
#4  01:00:15 Preview Owner requested (P0301 active) · 01:00:22 claimed 1/3 · 01:00:28 draft_ready 5594 ms
    01:50:13 QA Tech approved {"edited_by_technician":true,"saved_to_job":true}
    (01:50:02 QA Office approve -> 403, reject -> 403: no event written, state unchanged)
#5  01:09:10 requested · 01:09:18 claimed 1/3 · 01:09:24 draft_ready 5656 ms
#6  01:09:11 requested · claimed 1/3 · invalid_output code_status_changed:P0301 · retry_scheduled
    · claimed 2/3 · invalid_output · retry_scheduled · claimed 3/3 · invalid_output · failed (01:11:13)
    01:15:16 Preview Owner requeued · claimed 1/3 · invalid_output code_status_changed:P0301:not supplied
    · retry_scheduled · claimed 2/3 · invalid_output · retry_scheduled · claimed 3/3 · invalid_output · failed
    01:22:32 Preview Owner cancelled
#7  01:22:32 requested · 01:22:41 claimed 1/3 · 01:22:47 draft_ready 5702 ms
#8  01:27:28 requested · 01:27:33 claimed 1/3 · 01:27:39 draft_ready 5327 ms
#9  01:50:22 QA Tech requested · [worker down 01:37–01:52] · 01:52:52 claimed 1/3 · 01:53:19 draft_ready 27000 ms
#10 01:50:22 QA Tech requested · [worker down] · 01:53:19 claimed 1/3 · 01:53:25 draft_ready 5391 ms
#11 01:57:43 QA Tech requested while offline · 02:00:20 claimed 1/3 · 02:00:26 draft_ready 6250 ms
```

## What the AI produced on the swap tests (guardrails held; reasoning wrong)

| Swap result in notes | Correct reading | AI's top suspected cause | Proposed repair |
|---|---|---|---|
| Misfire **followed** coil 1 to cyl 3 (#4, #5, #8, #9, #11) | the original cyl-1 coil is bad; wiring unlikely | "Faulty ignition coil 3 / in cylinder 3" (high) + cyl-3 wiring (medium) | none |
| Misfire **stayed** on cyl 1 (#7, #10) | coil ruled out; plug, injector, wiring or mechanical at cyl 1 | "Cylinder 1 ignition coil faulty" (high) + cyl-1 wiring (medium) | none |

The status was always kept as supplied, the confirmed finding was copied verbatim from the tech note, and no repair was proposed. From `e45ad23` onward every swap draft carries the automatic swap caution.

## Other live checks

- **Offline/restart:** worker restarted at 01:52:52, and #9/#10 were claimed once each. The worker was stopped at about 01:54. The job page showed "Workstation offline — last check-in 01:53" at 01:57:42. Restored at about 02:00; #11 was claimed once.
- **Outage root cause:** at 01:37 the worker exited with task result `0xC000013A` (its console was closed). Fixed by the window-less `pythonw.exe` task plus a 5-minute watchdog. Watchdog restart: killed 02:14:12, back 02:14:48 (worker.log pid 13824 → 28120).
- **Rollback switch:** with `LOCAL_NODE_ENABLED=false` (02:46), the node API returned 503 and the worker backed off. `/readyz` reported ready, job pages worked with no AI panel, and approved findings stayed in the notes. With `true` again, the worker resumed by itself at 02:51:33.
- **Database objects on `Postgres-ltXm`:** 4 checks, the `uq_diag_review_one_active_per_ro` partial index, and 3 triggers.
