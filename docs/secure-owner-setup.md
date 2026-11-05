# Secure owner login recovery for Imperial Operations

The production service has one existing `users` row but no self-service login recovery. This feature claims that **same existing user ID**, preserving historical customer data, account links, repair orders and payments. It never creates a default admin.

## Owner-only setup steps

1. Sign in to your own [Railway dashboard](https://railway.com/dashboard).
2. Open **respectful-charm** project and choose the **web** service (not `shop`/messaging).
3. Open **Variables**. Add the variable **`IMPERIAL_OWNER_SETUP_TOKEN`** with a unique, random 32+ character secret that you generate yourself in your password manager. Do not send the code or your password in ChatGPT or email.
4. Apply the variable change and confirm the `web` deployment is healthy.
5. Open **https://web-production-94989.up.railway.app/owner-setup** on your phone or computer.
6. Enter the secret, choose a private username and a unique password of at least 14 characters. Submit once. A successful claim signs you in and opens `/finance/`.
7. Return to Railway and **delete `IMPERIAL_OWNER_SETUP_TOKEN`**. Let the service finish redeploying.
8. For normal visits use **https://web-production-94989.up.railway.app/login**, then the **Finance** menu.

## Controls and caveats

- The private variable exists only in the `web` Railway service environment and is never committed to Git.
- Claim is allowed exactly once: the existing user is marked with role `owner`, and setup remains disabled permanently for that existing account.
- Claim rejects multiple pre-existing users, already-claimed owner accounts, invalid CSRF, unconfigured or too-short secrets, and weak/oversized passwords.
- In recovery read-only mode, **only POST /owner-setup** is exempted and still requires the private token + CSRF. No other customer, money, estimate, or dispatch mutations are unlocked.
- Owner role is recognized by existing administrator-only pages. Non-owner accounts cannot access `/finance/`.
- Recovery uses Railway project access as proof of ownership; it is **not email verification**. Do not present it as an emailed one-time passcode or OAuth login.
- The initial Finance screen is operational-only. The historical portfolio must still be securely imported and reconciled before showing verified business profit.

If login recovery is needed again, use an authenticated administrator/recovery process rather than re-enabling this one-time claim. Do not introduce hard-coded credentials or public reset links.
