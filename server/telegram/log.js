"use strict";

// Idempotency ledger + audit log for both Telegram channels. Storage: server/store.js
// (telegram_updates, telegram_log - see its COLLECTIONS comment).
//
// Webhook delivery (not polling) means Telegram, not us, decides when an update is sent; it
// retries a delivery that didn't get a fast 2xx. telegram_updates is the idempotency guard against
// that retry ever being processed twice. telegram_log is the audit trail requirement (ТЗ п.6.9):
// the original message plus what we decided to do with it, kept for both channels so "Интеграции
// → Telegram" can show real status without re-deriving it from business data on every request.

const store = require("./../store");

const MAX_LOG = 2000;

function nowIso() {
  return new Date().toISOString();
}

/** True if this update_id was already processed (and records it as seen, atomically) - call once,
 *  right at the top of handling an update, before anything else happens. */
function claimUpdate(updateId) {
  const id = String(updateId);
  return store.tx((s) => {
    if (s.telegram_updates.some((u) => u.id === id)) return false;
    s.telegram_updates.push({ id, processed_at: nowIso() });
    return true;
  });
}

/**
 * Record one processed (or queued, or rejected) update for audit + the status page.
 * entry: {channel: "personal"|"engineering", chat_id, user_id, update_id, message_text,
 *         outcome: "created"|"updated"|"listed"|"replied"|"queued"|"ignored"|"rejected"|"error",
 *         detail, error}
 */
function record(entry) {
  store.tx((s) => {
    s.telegram_log.push(Object.assign({ at: nowIso() }, entry));
    if (s.telegram_log.length > MAX_LOG) s.telegram_log.splice(0, s.telegram_log.length - MAX_LOG);
  });
}

function recent(channel, limit = 50) {
  return store.read((s) => s.telegram_log
    .filter((e) => !channel || e.channel === channel)
    .slice(-limit).reverse());
}

/** Summary counters for the "Интеграции → Telegram" status page - derived from the log itself
 *  (never a separately-incremented counter that could drift from what actually happened). */
function summary(channel) {
  return store.read((s) => {
    const entries = s.telegram_log.filter((e) => e.channel === channel);
    const last = entries[entries.length - 1] || null;
    const errors = entries.filter((e) => e.outcome === "error").length;
    const byOutcome = {};
    entries.forEach((e) => { byOutcome[e.outcome] = (byOutcome[e.outcome] || 0) + 1; });
    return { processed: entries.length, errors, by_outcome: byOutcome, last_message_at: last ? last.at : null,
      last: last || null };
  });
}

module.exports = { claimUpdate, record, recent, summary };
