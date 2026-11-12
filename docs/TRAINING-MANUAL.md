# Imperial Auto Care training manual

**For employees and for Imperial (the AI).** One manual and one set of steps for both. Pricing rules come from `docs/PRICE-SHEET.md`, and decisions are recorded in `docs/REQUIREMENTS-REGISTER.md`. If this manual disagrees with either of those, they win, and this manual gets fixed.
Add new chapters here. Don't start a second manual. This file moves into the Knowledge Center (R29) once that exists.

> **Status (Nov 11, 2026):** documentation only; operational examples and vendor listings are historical and require fresh verification. Imperial's live text and draft features do **not** read this file yet. Wiring it into Imperial is tracked under R29.

---

## 1. Writing a repair estimate from a diagnosis

**Goal:** a written estimate the customer can approve, with every price labeled and nothing guessed.
**Who:** an advisor or the owner. Imperial may prepare it. Complex work, unusual vehicles and price disagreements need the **owner's approval before sending** (price sheet rule 6).

### Step 1. Identify the exact vehicle (never skip this)
1. Get the VIN. A scan report may provide VIN, model, engine, mileage and DTCs, but confirm the identifiers against the vehicle and repair record. Report sender, filename and included fields vary; never assume all are present.
2. Check the trim and powertrain from the report, not from the customer's description. Example: the customer said "2013 Fusion", but the report said **Fusion SE Hybrid, 2.0L, 259,572 mi**. A hybrid uses different fuel tank, filler neck and cooling parts, so pricing a regular Fusion would have been wrong.
3. Write down the mileage and every code, including ones that aren't part of the complaint, so the customer can be told about them separately.

### Step 2. Look up labor and OEM parts in ALLDATA
1. Sign in at my.alldata.com (each person uses their own login; never share or store passwords). Go to **Repair**, then enter the VIN or pick it from **Recent Vehicles**.
2. Review any available recall and service-history information and document the source and date checked. A historical example is not a current recall clearance.
3. Click **Parts and Labor**, then open each component you're replacing. Write down:
   - The **standard** labor hours. Quote standard hours, not warranty hours (warranty hours are lower and are what Ford pays dealers).
   - Every **Includes / Does not include** note. Anything listed as "not included" is a separate line or must be listed as an exclusion.
   - The OEM part number and list price, picking the line that matches the trim (for example, *FWD, without plug-in*).
4. Watch for overlap. If two jobs share disassembly (a fuel tank and the filler neck), ask the owner whether to reduce the combined time. Don't quietly add both in full or quietly cut them.
5. **Do not** apply the "1.3x" multiplier to ALLDATA hours. It is unverified (price sheet section 1) until the owner confirms it.

### Step 3. Look up aftermarket parts in AutoZonePro
1. Sign in at autozonepro.com. Type the **VIN** in the search bar so every result is matched to the vehicle.
2. Search with catalog words. "fuel tank" returned portable generators. Use **Browse Parts**, then the category (e.g. Fuel & Air Delivery).
3. If the part isn't listed (the Fusion Hybrid had no tank or filler neck), stop after a couple of tries. The source is then the **Ford dealer (OEM)** or a **used/recycled part (LKQ)**. Write down which one you used.
4. Write down the price, brand, warranty and delivery date. Never place an order without the owner's approval.

### Step 3b. Check local salvage yards on car-part.com (used parts)
1. Go to car-part.com. Set the year, make/model and part, enter ZIP **85307**, and search.
2. Pick the exact trim/engine on the next screen (e.g. **Hybrid SE**). Results are sorted by distance and show grade, stock #, price and phone.
3. Prefer listings whose trim matches the car (the word "Hybrid", or "VIN U (8th digit)") and Grade A, at the closest yards. Write down the yard, stock #, price and phone.
4. Historical Fusion listing, Nov 9 (not confirmed current inventory): LKQ-Keystone Phoenix stock P12025, 2014 Fusion Hybrid SE, Grade A, $133.50 (17 mi). Also Matador (M677, $100) and United #1 (5952, call).

### Step 3c. Office confirms availability (before the estimate goes out)
1. The advisor records a **[PARTS CHECK]** follow-up in the approved job/task workflow (or Quo if supported) with the yards, stock numbers, prices, phone numbers and the part number to quote.
2. The office (Mom) calls and confirms: the part is in stock and already off the donor car, it's the right trim (e.g. hybrid), its condition, the price, the warranty and the pickup time. She writes the result in the task.
3. **Nothing is ordered** until the owner approves the estimate.

### Step 4. Build the estimate
| Line | Rule |
|---|---|
| Labor | ALLDATA standard hours × the current rate: **$150** shop, $175 diesel, **$165** if the customer supplies the parts |
| Parts | Supplier price. No markup has been approved (price sheet), so get the owner's OK before adding one |
| Tax | Use the currently approved tax configuration; 9.2% parts-only is a documented working reference (R12), pending accountant verification of labor, sublet and shop-fee treatment |
| Diagnostics | Further testing by approval: $150 advanced in-shop diagnostic. Describe each test in plain words |
| Exclusions | Everything ALLDATA says is "not included", plus anything not yet diagnosed |
| Customer-supplied parts | Labor at $165/hr, no warranty on their parts, written authorization |
| Deposit | Do not quote a deposit percentage or threshold unless confirmed in current approved payment terms |
Label every price **confirmed current**, **working promotional**, **historical reference** or **custom quote required**.

### Step 5. Timeline and communication
- **Order of approval:** draft the estimate → the owner approves it → it's sent to the customer → the customer approves → parts are ordered. Drafts are marked "DRAFT — FOR OWNER APPROVAL".
- Recommend cheap diagnostics first on high-mileage cars (e.g. misfire testing before a tank), so the customer doesn't buy parts for an engine that isn't worth fixing.
- Give the parts arrival date from the supplier and the labor time from ALLDATA, then add the shop's actual schedule.
- Promise a regular update time (for example, a daily text by 5 PM) and keep it. The Fusion complaint on Nov 9 was about missed updates, not the repair.
- Authorized advisors may send standard estimates under the current price sheet; complex repairs, scope changes and material price discrepancies require owner approval before sending. Customer messaging must follow current channel permissions and approved workflows. Before sending, re-read the Quo conversation to avoid duplicate or outdated messages.

### Worked example: 2013 Fusion SE Hybrid fuel leak plus misfire (Nov 9, 2026)
| Item | Source | Value |
|---|---|---|
| Fuel tank R&R | ALLDATA | 3.5 hr standard (2.5 warranty); draining the tank not included |
| Filler neck R&R | ALLDATA | 0.6 hr standard (not used: neck reused) |
| Fuel tank, OEM EG9Z9002F | ALLDATA list | $847.30 |
| Filler neck, OEM DG9Z9034R (FWD, non-plug-in) | ALLDATA list | $385.00 |
| Aftermarket tank or neck | AutoZonePro | Not listed for this vehicle (searched "fuel tank", "gas tank", Fuel & Air Delivery category) |
| Tank or filler hose for this car | AutoZonePro | No match for "fuel filler hose" or "fuel tank filler neck hose". Measure the hose's inside diameter and buy universal fuel filler hose by size |
| Used tank | LKQ Online | None listed Nov 9. Call a local LKQ yard and give the hybrid part number EG9Z9002F |
| Confirmed scope | Owner, from inspection | Tank plus the rubber filler hose between tank and neck. The filler neck itself is reused (ALLDATA's neck assembly DG9Z9034R is **not** needed) |
| Misfire testing P0303/P0316 | Price sheet | $150 advanced in-shop diagnostic |

Misfire testing plan for one cylinder: live misfire counters and fuel trims → swap that cylinder's coil and plug to another cylinder (does the misfire follow?) → inspect the plug → compression test (leak-down if low) → injector check → look for vacuum leaks. **Hybrid:** the engine only runs when the computer commands it, so use the scan tool's engine-running service mode. Follow ALLDATA's high-voltage safety steps before any underbody work near hybrid wiring.
