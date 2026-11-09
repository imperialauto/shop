# Imperial design system (R36)

One look for every Imperial screen: the public website, booking and estimate forms, customer status and approval pages, the owner dashboard, staff and technician screens, finance, Imperial AI, the Knowledge Center, divisions, and alerts.

**Owner direction (Nov 9, after rollout step 1):** "purple, grey and gold": a deeper purple-grey base mixed between purple and grey so the gold pops, with several tones rather than one colour. Tokens below are superseded by `app/static/imperial.css`: header #16111C, page #221C29, cards #2C2534, raised #362E40, lines #463C52, text #F1EDF4, grey text #B4ABBF, brand purple #2D0245 and #5A3A78 for fills and active items, gold #EAD272 for titles, primary buttons and the active page.

**Status:** proposed. The preview needs owner approval before production templates change. Until then, the website keeps `app/static/site.css`, and the dashboard keeps the black and gold look in `app/templates/base.html`.

## Audit (Nov 9)
| Surface | Today | Problem |
|---|---|---|
| Public website (`/site`, `site.css`) | Purple #2D0245 header and hero, gold #EAD272 and #CBB06B, light paper #FAF8FB, serif headings, 10–14 px radii, 44–48 px touch targets | Matches the logo and the shop's branding rules. This is the base |
| Dashboard and staff (`base.html`, Tailwind CDN) | Black #09090B, charcoal #18181B, muted gold #D4A853, all sans, rainbow status badges (blue, orange, green, yellow, violet, teal), some `plum-*` classes that don't exist | Looks like a different product. Gold is a different shade. Status colors don't follow the brand. Missing classes fall back to defaults |
| Customer pages (sign, status, complete) | Mixed: some use the dashboard look and some are plain | A customer moves from the purple site to a black page |
| Estimates and invoices (PDF) | Purple and gold per the shop's branding rules | Already aligned |

## Tokens
One stylesheet, `app/static/imperial.css`, defines these. Both the site and the dashboard load it, and nothing else defines brand colors.

| Token | Value | Use |
|---|---|---|
| `--imp-purple` | #2D0245 | Brand. Headers, sidebar, primary buttons on light surfaces |
| `--imp-purple-deep` | #1A0129 | Footer, sidebar background, pressed states |
| `--imp-purple-soft` | #3D0A5C | Hover on purple, selected nav |
| `--imp-purple-tint` | #F3EEF8 | Selected rows, info notes |
| `--imp-gold` | #EAD272 | Accent on purple. Call-to-action buttons, active nav, focus ring |
| `--imp-gold-dark` | #CBB06B | Gold on white (eyebrows, borders, icons). Never body text |
| `--imp-gold-tint` | #FFFBEA | Highlight panels, "needs you" cards |
| `--imp-ink` | #1D1724 | Body text |
| `--imp-muted` | #5D5468 | Secondary text (contrast 7:1 on paper) |
| `--imp-line` | #E7E1EC | Borders and dividers |
| `--imp-paper` | #FAF8FB | Page background, site and dashboard alike |
| `--imp-surface` | #FFFFFF | Cards, tables, forms |
| `--imp-ok` / `--imp-warn` / `--imp-danger` | #17603A / #8A5A00 / #9B1C1C | Status text, each with a light tint background |
| `--imp-serif` | Cormorant Garamond, Georgia | Page titles, card titles, the brand name |
| `--imp-sans` | System UI stack | Everything else |
| `--imp-radius` | 10 px controls, 14 px cards | |
| `--imp-touch` | 44 px minimum, 48 px for primary actions | |

**Contrast:** gold text appears only on purple. On white, use `--imp-gold-dark` for decoration and `--imp-purple` for text. Body text is ink on paper.

## Components
- **Buttons:** primary is purple with white text. Call-to-action is gold with deep purple text. Secondary is a white button with a purple outline. Danger is a white button with a red outline. They're the same height everywhere.
- **Cards:** white with a 1 px line border and 14 px radius. The title is serif purple. A highlight card has a gold tint and a gold left border.
- **Tables:** white with a line border. Headers are small-caps muted text. On phones each row becomes a card (the dashboard already does this; keep it).
- **Forms:** one input style for the public site and the dashboard: 48 px, 10 px radius, purple focus border and a gold focus ring.
- **Status chips:** one set across jobs, queue, approvals and invoices:
  - **Neutral** (intake, routine): purple tint.
  - **Waiting** (waiting on parts, waiting on approval, needs owner): gold tint with dark gold text.
  - **Working** (diagnosing, in progress): purple with white text.
  - **Done** (complete, delivered, paid, approved): green tint.
  - **Problem** (declined, failed, overdue, urgent): red tint.
- **Navigation:**
  - **Dashboard:** a deep purple sidebar on desktop, with gold active item and serif brand at the top. On phones, a purple top bar and a white bottom tab bar with a gold active tab.
  - **Website:** keeps its purple top bar.
- **Alerts and notices:** the same four note styles everywhere (info, ok, warn, error).

## Divisions
- **Imperial Auto Care:** purple and gold, as above.
- **NVZN Customs:** the same layout and components. Its pages and records carry the NVZN wordmark in gold on near-black (#14101A) for the hero and badge, with the line "a division of Imperial Auto Care". Buttons, forms and navigation stay Imperial.
- **Fleet, rentals and roadside:** no separate theme. They use a labeled chip (Fleet, Rental, Roadside) on records.

## Rollout (after approval)
1. Add `imperial.css`. Point `site.css` at the shared tokens; the website looks the same.
2. Restyle `base.html` (dashboard shell, buttons, cards, tables, badges). Every page inherits it, and no route or form changes.
3. Restyle the customer pages (sign, status, complete), then finance, alerts, settings and My day.
4. Run the phone and desktop walkthrough after each step. Rollback is reverting the stylesheet PR, because no data or routes change.
