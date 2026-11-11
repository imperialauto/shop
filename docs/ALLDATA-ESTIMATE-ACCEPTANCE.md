# Imperial Operations: ALLDATA-to-customer acceptance test and user guide

**Status: TEST PLAN — NOT YET PASSED.** Owner request, Nov 9, 2026. Related: issue #109; requirements R02, R03, R06, R08, R10, R13, R37, R38.

## Purpose

Use a documented, authorized ALLDATA research case with verified labor evidence to prove the customer-to-shop workflow. Do not assume a prior browser session is still available. A completed synthetic booking and displayed reference alone does not prove that the actual repair estimate is correct or that the owner avoids manual reentry.

Do **not** assume an ALLDATA browser login is an automated ALLDATA integration. Record separately what the staff member manually researched versus what Imperial itself retrieved, calculated, populated or sent.

**Release boundaries:** Run in private, isolated QA with synthetic customer names, phone numbers, VINs/vehicle identifiers permitted for testing, and disposable appointments. Never send a real message or approval link, charge a card, create real bookings, or use the production DB. Public forms stay off in production; Squarespace stays live.

## Test case selection and price evidence

Prefer a previously documented, verifiable vehicle/job case; independently recheck labor, parts, availability and scope before testing. Use synthetic customer identifiers and isolated QA records, not production customer data.

Record these facts **before** generating any estimate:

| Field | Evidence to capture |
|---|---|
| Test ID, timestamp, QA URL, code SHA | ______ |
| Vehicle: year, make, model, engine, VIN/identifier if permitted | ______ |
| Symptom / requested repair / selected service | ______ |
| ALLDATA labor operation and hours | ______ |
| ALLDATA page/section reference and date consulted | ______ |
| Parts description, SKU, quantity, fitment, live vendor | ______ |
| Part availability and cost (AutoZonePro or approved vendor) | ______ |
| Shop / mobile / diesel / customer-supplied-parts rate | ______ |
| Service call, travel and other authorized fees | ______ |
| Tax treatment and any required owner review | ______ |
| Person or system preparing the quote and time spent | ______ |

Do not reproduce or upload an ALLDATA manual. Retain only necessary labor-time facts, operation labels, source references, and an access-controlled audit record. Avoid public screenshots containing private customer information or vendor credentials.

Use `docs/PRICE-SHEET.md` and current editable business settings. Current reference hourly rates: shop $150, mobile $165, diesel $175, CSPR shop $165; mobile service call $75 **once**, plus a separately verified travel charge. No approved universal parts markup. Never automatically stack hourly labor onto a labor-included flat package. Verify the correct scope, shop fee, parts tax, and whether a final price or only a starting estimate is defensible.

## Known owner-observed UX issues to retest (not yet independently verified)

- After selecting a booking time, the dropdowns remain open and the form continues as one long page instead of advancing to the next step. Test progressive disclosure, focus movement, back navigation and mobile scrolling; record clicks and completion time.
- From the customer status link, the owner reported no obvious route back to the prior page or homepage. Verify visible Back and Home navigation without exposing private data or breaking tokenized access.
- A synthetic booking was completed and a reference displayed in the preview. This is owner-observed evidence only, not proof that backend persistence, pricing, scheduling, notifications or ALLDATA integration passed.
- Verify how customers retrieve or locate their booking reference when revisiting status lookup.

## Acceptance test: actual customer-to-estimate workflow

Run once at desktop width and once at phone width. Record every URL, click, duplicate field entry, manual handoff and failed step.

| Step | Action in QA | Required evidence / pass condition |
|---|---|---|
| 1. Service entry | Open `/site/`, select the relevant service | Correct self-service route; **not** an unavoidable Call button |
| 2. Customer intake | If price is known, use `/site/book`; if vehicle-specific/uncertain, use `/site/estimate` | Vehicle, symptom, location, customer/contact method collected once; VIN optional where valid |
| 3. Confirmation | Submit the synthetic request | One reference and confirmation; distinguish confirmed booking from estimate request; verify reference retrieval guidance and next action |
| 4. Persistence | Refresh confirmation and open `/site/status` | Reference persists; authorized reference/phone lookup works; private notes not exposed |
| 5. Office intake | Open `/dashboard` or the customer/RO view as a permitted staff login | Exactly one customer, one vehicle, one job/lead and one actionable owner/advisor task; no retyping intake |
| 6. Labor verification | Use the existing ALLDATA browser case | Labor operation/hours match the saved test evidence; note whether entered manually or integrated automatically |
| 7. Parts verification | Cross-check approved vendor | Correct vehicle fitment, live part cost/availability and quantities; uncertain items clearly flagged |
| 8. Draft estimate | Open `/estimates/` or the appropriate repair-order estimate workflow | Line items reflect verified hours × current rate, real parts, taxes and disclosed fees; no guessed parts or duplicate call-out |
| 9. Authorization | Apply the standard-vs-complex estimating permission rules | Complex, unverified or changed scope requires owner approval; no customer text automatically sent |
| 10. Customer approval | In QA only, open the authorized test `/sign/ro/{token}` page for pending line items | Line-by-line approve/decline, drawn signature, non-cached private page; no real customer delivery |
| 11. Scheduling | If service is bookable, select a test slot, then inspect `/appointments/` | Shop hours, buffer, mobile travel, linked-calendar conflicts and duplicate submissions enforced |
| 12. Status & closeout | Open test tracking link and relevant RO/job screen | Consistent status and authorized next action; visible Back/Home navigation; no staff-only notes on customer page |

### Required negative tests

1. Unpriced/unsupported services still permit **online estimate intake** in QA without making the customer call.
2. Submitting the same form twice does not create duplicate jobs, queue items or appointments.
3. A timed event on the linked calendar blocks the slot; an unreadable linked calendar offers no bookable times, and neither failure silently confirms a booking.
4. A tech/advisor cannot bypass an owner approval requirement; a public user cannot see private RO information or customer attachments.
5. A wrong reference/phone does not expose a record or permit guessing a job's existence.
6. No outbound SMS, charges, actual appointments or production settings are touched.

## Pass/fail rubric

- **PASS — customer intake:** A customer can submit a standard booking or complex estimate request without calling; request is saved and trackable.
- **PASS — verified estimate:** Labor, parts and calculations exactly match the documented ALLDATA/vendor case; the estimate appears on the correct RO, with approval rules observed.
- **PASS — workflow:** Staff can go from request to completed draft without copying customer details; line approvals and status update the same record.
- **PASS — automation claim:** Claim `automated estimate draft` only when Imperial itself populates verified estimate fields from authorized connected data or deterministic pricing rules. If Claude manually looked up ALLDATA and entered the values, label it `staff-assisted estimate — manual lookup`.
- **FAIL/BLOCKED:** Any required step falls back to calling, invents prices, loses customer data, creates duplicates, requires unauthorized approval bypass, or cannot associate the estimate with the original request. Record the exact blocker and fix it; do not redefine PASS.

The currently existing synthetic E2E suite (`tests/e2e/walkthrough.py`, `tests/public_booking_postgres.py`) covers booking and estimate **intake**. It does **not** by itself establish this end-to-end verified estimate/approval handoff or ALLDATA browser integration.

## Usage guide to produce while running the test

Record the **actual** screens, not mockups:

**Customer guide**: `/site/` → service → Book or Estimate → vehicle/contact form → reference → status → approval if offered. State clearly when the displayed price is final versus starting, and when the request is not a confirmed appointment.

**Service advisor guide**: `/dashboard` → new queue item/customer file → original vehicle and complaint → ALLDATA labor reference and approved vendor part lookup → draft estimate → verify line items/rates/tax → owner approval when required → customer approval (with authorized communication) → appointment and RO status update. Do not instruct advisors to share credentials or copy customer data into another app unnecessarily.

**Owner guide**: review only exceptions, unverified labor/parts, complex jobs, customer communication approval and financial commitments; confirm a single next action and audit record. A manual action should be explicitly documented and flagged for later automation.

For each guide step capture: URL/screen name, action performed, screenshot with synthetic data, required role, expected output, and any confusing extra click. Include a phone screenshot and desktop screenshot for the same key steps. Store screenshots in the private CI artifact or authorized documentation area, not a publicly indexed repository if they contain tokens or business credentials.

### Execution record (complete after the live QA walkthrough)

- Test case / ALLDATA reference: **NOT RECORDED**
- QA URL / exact commit: **NOT RECORDED**
- Approved parts/rate calculation: **NOT RECORDED**
- Customer flow result: **OWNER REPORTED synthetic preview booking completed and reference displayed; full independent walkthrough NOT RUN**
- ALLDATA-to-estimate result: **NOT RUN**
- Approval and scheduling result: **NOT RUN**
- Total steps, duplicated entries and manual handoffs: **NOT MEASURED**
- Phone and desktop evidence locations: **NOT RECORDED**
- Gaps filed, code PRs and linked issues: **Owner-reported booking auto-advance and status-page navigation defects; issue/PR links NOT RECORDED**
- Owner acceptance: **NOT REQUESTED**

**Do not mark R02/R03/R10/R37/R38 `verified` until the corresponding test evidence exists.**
