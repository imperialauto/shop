// ─────────────────────────────────────────────────────────
// Verifies a Quo webhook request against WHICHEVER signature scheme it
// arrives with, then parses the body and hands off to the route handler.
//
// This supersedes src/lib/verifyQuoSignature.js for actual production use.
// That file is kept for reference, but has one known bug this version
// fixes: it re-serializes the already-parsed `req.body` with
// JSON.stringify() to check the signature, instead of using the exact
// raw bytes Quo signed. If Express/JSON re-serialization ever produces
// different bytes than what was sent (key order, whitespace, unicode
// escaping), that check silently fails valid webhooks. This version
// verifies against the raw request body buffer instead, for both schemes.
//
// Requires the route to be mounted with express.raw(), NOT express.json():
//   router.post("/", express.raw({ type: "*/*" }), verifyQuoWebhook, handler);
//
// ── Scheme A: legacy `openphone-signature` header ──
//   Format: hmac;<version>;<timestamp>;<base64 signature>
//   Signed content: `${timestamp}.${rawBody}`
//   Secret: base64-decoded signing secret from Quo → Settings → Webhooks
//
// ── Scheme B: beta Standard Webhooks-compatible headers ──
//   Headers: webhook-id, webhook-timestamp, webhook-signature
//   Signed content: `${webhook-id}.${webhook-timestamp}.${rawBody}`
//   Secret: `whsec_...` — strip the `whsec_` prefix, base64-decode the rest
//   webhook-signature may contain multiple space-delimited `v1,<sig>` entries
//     (key rotation) — a match against ANY of them is valid
//
// Confirm in your Quo webhook settings which scheme your subscription
// actually uses before going live — set QUO_WEBHOOK_SCHEME=legacy or
// QUO_WEBHOOK_SCHEME=standard to skip auto-detection, or leave unset to
// auto-detect from whichever header is present.
// ─────────────────────────────────────────────────────────

const crypto = require("crypto");

const MAX_CLOCK_SKEW_SECONDS = 5 * 60;

function timingSafeEqualStr(a, b) {
  const bufA = Buffer.from(a, "utf8");
  const bufB = Buffer.from(b, "utf8");
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

function verifyLegacy(rawBody, header, secret) {
  const [scheme, , timestamp, providedDigest] = header.split(";");
  if (scheme !== "hmac" || !timestamp || !providedDigest) {
    return { ok: false, reason: "Malformed openphone-signature header" };
  }

  // Quo's legacy timestamp is milliseconds since epoch (confirmed from
  // their own documented example: "hmac;1;1639710054089;..."), NOT
  // seconds — Date.now() is already ms, so compare directly.
  const skew = Math.abs(Date.now() - Number(timestamp));
  if (skew > MAX_CLOCK_SKEW_SECONDS * 1000) {
    return { ok: false, reason: "Signature timestamp outside tolerance" };
  }

  const signedContent = `${timestamp}.${rawBody}`;
  const keyBytes = Buffer.from(secret, "base64");
  const computedDigest = crypto.createHmac("sha256", keyBytes).update(signedContent, "utf8").digest("base64");

  return timingSafeEqualStr(providedDigest, computedDigest)
    ? { ok: true }
    : { ok: false, reason: "Invalid signature (legacy)" };
}

function verifyStandard(rawBody, webhookId, webhookTimestamp, signatureHeader, secret) {
  if (!webhookId || !webhookTimestamp || !signatureHeader) {
    return { ok: false, reason: "Missing Standard Webhooks headers" };
  }

  const skew = Math.abs(Date.now() / 1000 - Number(webhookTimestamp));
  if (skew > MAX_CLOCK_SKEW_SECONDS) {
    return { ok: false, reason: "Signature timestamp outside tolerance" };
  }

  const signedContent = `${webhookId}.${webhookTimestamp}.${rawBody}`;
  const secretBytes = Buffer.from(secret.replace(/^whsec_/, ""), "base64");
  const computedDigest = crypto.createHmac("sha256", secretBytes).update(signedContent, "utf8").digest("base64");

  const candidates = signatureHeader.split(" ").map((s) => s.split(",")[1]).filter(Boolean);
  const match = candidates.some((candidate) => timingSafeEqualStr(candidate, computedDigest));

  return match ? { ok: true } : { ok: false, reason: "Invalid signature (standard)" };
}

function verifyQuoWebhook(req, res, next) {
  try {
    const rawBody = Buffer.isBuffer(req.body) ? req.body.toString("utf8") : req.body;
    if (typeof rawBody !== "string") {
      return res.status(400).send("Expected raw request body — mount this route with express.raw()");
    }

    const forcedScheme = process.env.QUO_WEBHOOK_SCHEME; // "legacy" | "standard" | undefined
    const legacyHeader = req.headers["openphone-signature"];
    const standardSigHeader = req.headers["webhook-signature"];

    const useStandard = forcedScheme === "standard" || (!forcedScheme && !legacyHeader && standardSigHeader);
    const useLegacy = forcedScheme === "legacy" || (!forcedScheme && legacyHeader);

    let result;
    if (useLegacy) {
      const secret = process.env.QUO_WEBHOOK_SIGNING_SECRET;
      if (!secret) throw new Error("Missing QUO_WEBHOOK_SIGNING_SECRET env var");
      if (!legacyHeader) return res.status(401).send("Missing openphone-signature header");
      result = verifyLegacy(rawBody, legacyHeader, secret);
    } else if (useStandard) {
      const secret = process.env.QUO_WEBHOOK_SIGNING_SECRET_STANDARD || process.env.QUO_WEBHOOK_SIGNING_SECRET;
      if (!secret) throw new Error("Missing QUO_WEBHOOK_SIGNING_SECRET_STANDARD env var");
      result = verifyStandard(
        rawBody,
        req.headers["webhook-id"],
        req.headers["webhook-timestamp"],
        standardSigHeader,
        secret
      );
    } else {
      return res.status(401).send("No recognized Quo signature header present");
    }

    if (!result.ok) {
      console.error("Quo webhook signature rejected:", result.reason);
      return res.status(401).send(result.reason);
    }

    req.body = JSON.parse(rawBody);
    next();
  } catch (err) {
    console.error("Quo signature verification error:", err);
    return res.status(401).send("Signature verification failed");
  }
}

module.exports = { verifyQuoWebhook };
