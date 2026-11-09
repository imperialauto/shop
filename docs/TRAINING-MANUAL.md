# Imperial Auto Care training manual

**For employees and for Imperial (the AI).** One manual and one set of steps for both. Pricing rules come from `docs/PRICE-SHEET.md`, and decisions are recorded in `docs/REQUIREMENTS-REGISTER.md`. If this manual disagrees with either of those, they win, and this manual gets fixed.
Add new chapters here. Don't start a second manual. This file moves into the Knowledge Center (R29) once that exists.

> **Status (Nov 9, 2026):** documentation only. Imperial's live text and draft features do **not** read this file yet. Wiring it into Imperial is tracked under R29.

---

## 1. Writing a repair estimate from a diagnosis

**Goal:** a written estimate the customer can approve, with every price labeled and nothing guessed.
**Who:** an advisor or the owner. Imperial may prepare it. Complex work, unusual vehicles and price disagreements need the **owner's approval before sending** (price sheet rule 6).

### Step 1. Identify the exact vehicle (never skip this)
1. Get the VIN. The fastest source is the scan report: the LAUNCH scanner emails it from `diagnostic_report@cnlaunch.com` to the shop Gmail. The file name is `MAKE_VIN_AllSystemDTC_<date>.pdf`, and it lists the VIN, model, engine, mileage and every code.
2. Check the trim and powertrain from the report, not from the customer's description. Example: the customer said "2013 Fusion", but the report said **Fusion SE Hybrid, 2.0L, 259,572 mi**. A hybrid uses different fuel tank, filler neck and cooling parts, so pricing a regular Fusion would have been wrong.
3. Write down the mileage and every code, including ones that aren't part of the complaint, so the customer can be told about them separately.

### Step 2. Look up labor and OEM parts in ALLDATA
1. Sign in at my.alldata.com (each person uses their own login; never share or store passwords). Go to **Repair**, then enter the VIN or pick it from **Recent Vehicles**.
2. Check the **Open Recalls** banner and the **Service History**, and note any open recall on the estimate. (Fusion: no open recalls; last service 06/03/2026 at 248,878 mi.)
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

### Step 4. Build the estimate
| Line | Rule |
|---|---|
| Labor | ALLDATA standard hours × the current rate: **$150** shop, $175 diesel, **$165** if the customer supplies the parts |
| Parts | Supplier price. No markup has been approved (price sheet), so get the owner's OK before adding one |
| Tax | **9.2% on parts only.** Labor is not taxed (R12) |
| Diagnostics | Further testing by approval: $150 advanced in-shop diagnostic. Describe each test in plain words |
| Exclusions | Everything ALLDATA says is "not included", plus anything not yet diagnosed |
| Customer-supplied parts | Labor at $165/hr, no warranty on their parts, written authorization |
| Deposit | 50% on jobs over $1,000 (check with the owner before quoting new terms) |
Label every price **confirmed current**, **working promotional**, **historical reference** or **custom quote required**.

### Step 5. Timeline and communication
- Give the parts arrival date from the supplier and the labor time from ALLDATA, then add the shop's actual schedule.
- Promise a regular update time (for example, a daily text by 5 PM) and keep it. The Fusion complaint on Nov 9 was about missed updates, not the repair.
- Every customer message waits for the owner's approval. Before sending, re-read the conversation in Quo so nothing is repeated or out of date.

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
