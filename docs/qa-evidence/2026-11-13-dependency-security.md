# Working audit: third-party parser/template minimum versions

Date: 2026-11-13 — **draft, disabled from production until owner-approved release**.

## Verified source and affected ranges

Current main pins `python-multipart==0.0.9`. GitHub's reviewed advisory GHSA-pp6c-gr5w-3c5g / CVE-2026-42561 reports a high-severity denial of service when a parser accepts unbounded multipart part headers; affected `<0.0.27`, patched `0.0.27`. FastAPI routes receive form submissions, so the dependency deserves prompt remediation. It is **not proof of exploitation or of any attack against Imperial**.

Current main also pins `jinja2==3.1.4`. GHSA-cpwx-vrp4-4pq7 / CVE-2025-27516 reports a sandbox issue in `<3.1.6`, patched `3.1.6`, that requires attacker-controlled template contents. We have **not** identified a route that lets outside users supply templates to execute, so do not overstate this application's exposure.

Sources:
- https://github.com/advisories/GHSA-pp6c-gr5w-3c5g
- https://github.com/advisories/GHSA-cpwx-vrp4-4pq7

## Small isolated change

- Pin `python-multipart==0.0.27` and `jinja2==3.1.6` in `requirements.txt`.
- Add `tests/test_dependency_security.py` to fail if vulnerable versions are reintroduced.
- Let the existing `shop-regression`, `quo-tests` and `app-acceptance-pr` GitHub Actions workflows test installation, Python API compatibility, public form, staff login, website booking and synthetic browser flows.

## Verification / release gates

1. Check CI on the **exact final SHA**, not an earlier commit, with Python 3.11; Railway's `runtime.txt` requests Python 3.12, so runtime-version compatibility must also be checked before deployment.
2. Confirm multipart parsing works for the actual FastAPI `Form` handlers with synthetics, not just that the package installs.
3. Run remaining dependency vulnerability inventory, including transitive packages; this patch does not claim a full software-composition audit.
4. Do not merge/deploy/modify Railway or customer data unless separately authorized.

Roll back code by reverting the PR **after** reviewing associated security exposure; reverting to vulnerable dependency pins is not a safe default rollback. Treat any production rollback as a separately reviewed maintenance action.
