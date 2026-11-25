// Website booking confirmation texts. The shop app decides which bookings get one (off unless the
// owner turns it on) and builds the wording; this only sends it. One send per booking, never
// retried, skipped if the customer ever texted STOP or the thread can't be read. No owner notice.

const { OPT_OUT } = require("./customerFollowups");

async function optedOut(quo, phone) {
  const result = await quo.listMessages({ participants: [phone], maxResults: 100 });
  if (!Array.isArray(result?.data)) return true;  // can't check, so don't send
  return result.data.some(message => /^in/i.test(message.direction || "") && OPT_OUT.test(message.text || message.body || ""));
}

async function sendBookingConfirmation({ confirmation, quo, shopClient }) {
  let status = "skipped";
  try {
    if (confirmation?.phone && confirmation?.text && !(await optedOut(quo, confirmation.phone))) {
      status = "failed";  // Never retry an ambiguous provider result.
      const sent = await quo.sendMessage({ to: confirmation.phone, content: confirmation.text });
      if (!sent?.id) throw Error("Quo did not return a message ID");
      status = "sent";
      await shopClient.logCommunication({ phone: confirmation.phone, direction: "outbound", body: confirmation.text,
        externalId: sent?.id, createdAt: sent?.createdAt });
    }
  } catch (error) {
    console.error("Booking confirmation:", error.message);
  }
  await shopClient.dispatchOutbound({ key: confirmation.key, status });
  return status;
}

module.exports = { sendBookingConfirmation };
