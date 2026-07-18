// ─────────────────────────────────────────────────────────
// Minimal real auth for this standalone service.
//
// This module is deployed as its own public Railway service (see the
// architecture note in prisma/schema.prisma for why: the shop app is
// Python/FastAPI, so this can't share its session/cookie auth). Without
// SOME gate, the dashboard page and API would be reachable by anyone who
// finds the URL — including real customer conversations. HTTP Basic Auth
// behind a single shared username/password is a deliberately simple fit
// for "one shop, a couple of coordinators," not a general-purpose auth
// system. If you later want per-user logins, swap this out — everything
// downstream just expects `req.user` to be set.
//
// Required in production: DASHBOARD_USERNAME + DASHBOARD_PASSWORD.
// In development, missing credentials fall through with a fixed dev user
// so local testing doesn't require setting them.
// ─────────────────────────────────────────────────────────

const crypto = require("crypto");

function timingSafeEqualStr(a, b) {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

function requireDashboardAuth(req, res, next) {
  const expectedUser = process.env.DASHBOARD_USERNAME;
  const expectedPass = process.env.DASHBOARD_PASSWORD;

  if (!expectedUser || !expectedPass) {
    if (process.env.NODE_ENV !== "production") {
      req.user = { id: "dev-user" };
      return next();
    }
    console.error("DASHBOARD_USERNAME / DASHBOARD_PASSWORD not set — refusing to serve unauthenticated in production.");
    return res.status(500).send("Server misconfigured: DASHBOARD_USERNAME/DASHBOARD_PASSWORD not set.");
  }

  const header = req.headers.authorization || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme !== "Basic" || !encoded) {
    res.set("WWW-Authenticate", 'Basic realm="Imperial Auto Care Messages"');
    return res.status(401).send("Authentication required.");
  }

  const [user, pass] = Buffer.from(encoded, "base64").toString("utf8").split(":");
  const ok = user && pass && timingSafeEqualStr(user, expectedUser) && timingSafeEqualStr(pass, expectedPass);
  if (!ok) {
    res.set("WWW-Authenticate", 'Basic realm="Imperial Auto Care Messages"');
    return res.status(401).send("Invalid credentials.");
  }

  req.user = { id: expectedUser };
  next();
}

module.exports = { requireDashboardAuth };
