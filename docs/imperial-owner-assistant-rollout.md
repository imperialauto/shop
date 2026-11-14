# Imperial owner assistant rollout

Imperial gives the owner one private place to understand conversations, decide what to do next, and track the work through completion. The dashboard is the main interface. Customer texting stays on Quo.

| Area | Verified starting point | Work remaining |
| --- | --- | --- |
| Tasks | PR #181 passed synthetic SMS and phone/desktop browser checks | Private QA and production acceptance, links to jobs and conversations, assignment and consolidated priorities |
| Recommendations | SMS has a queue snapshot and can fetch bounded customer history | Use current thread and call evidence, verified pricing and job status, and actionable draft replies in the dashboard |
| Scheduling | Existing calendar and bookings | Check resources, shop buffers and mobile travel before offering a time, then record confirmed bookings once and schedule reminders |
| Commitments | Existing task storage | Extract explicit call and text commitments, save evidence and due time, deduplicate retries, and detect overdue unsent estimates |
| Owner interface | Dashboard and Alert Center exist | Persistent owner-only chat with keyboard dictation, linked actions and digest, then authorized device push |
| Alerts | Scheduled owner SMS on hold after repeated reminders | One notification per material change, dashboard-only routine items, close or replace stale items, and tap through to the record |
| Navigation | Dashboard, My Day, Jobs and Alert Center overlap | Audit role-specific paths on phone and desktop before removing duplicate queues and tabs |

## Order of work

1. Load the customer context before generating an owner recommendation or interpreting a reply instruction.
2. Build private dashboard chat with persistent history and explicit links to a job or conversation. Reuse keyboard dictation first. Show evidence and freshness with each recommendation.
3. Connect confirmed appointments and explicit commitments to existing calendar and task write paths. Return success only after a stored record can be verified. Ask for missing details or approval where needed.
4. Build in-app notifications, with optional device push for urgent or due work. Reconcile alerts by conversation or job and close items that need no action. Keep SMS paused while validating the replacement.
5. Run phone and desktop QA for the full flow and simplify navigation using the results. Review the production release with the owner.

## Acceptance cases

- Fusion diagnostic-only request. Check the specific job and verified charge, draft the reply, and surface a concession for approval only if appropriate.
- Nathan duplicate alerts. A handled transfer or missed call marked as needing no reply stays off the action list unless a new actionable event arrives.
- Confirmed appointment. Detect conflicts before reserving, verify one appointment and one linked reminder after a retry.
- Promised estimate. Capture the commitment once with the call or text evidence and close the reminder when the estimate is sent.
- Privacy. Only authorized owners can see their assistant history and customer context. Customer sends keep the existing approval and context revalidation.
