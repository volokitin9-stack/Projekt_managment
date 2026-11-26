"use strict";

// Notification center for shturman-ko-analyst proposals (server/ko/proposals.js). This module owns
// exactly one thing - *read state* for a proposal, kept forever in its own collection
// (server/store.js's "ko_notifications", one row per proposal id, never deleted so "история
// уведомлений" always has the full record). It deliberately does NOT duplicate the proposal's own
// data (kind, project, summary, resolution...) - GET /api/ko/notifications joins each row against
// its review_queue item (server/reviewQueueModel.js) and returns that item's own fields alongside
// the read state, so "status" (ожидает/принято/отклонено) can never drift out of sync with the
// proposal itself: there is nothing to keep in sync, it is read live.
//
// Dedup: one row per proposal id (the review_queue item's own id, already deduplicated by
// dedupe_key in proposals.js). ensureForProposalIn() is a no-op if a row already exists, so calling
// it again for a re-submitted (merged-evidence) proposal, or re-running the startup backfill below,
// can never create a second notification for the same proposal.

const { v4: uuidv4 } = require("uuid");
const store = require("./store");

const KIND = "ko_proposal";

function nowIso() {
  return new Date().toISOString();
}

/** Create the notification row for a proposal if one doesn't exist yet. Idempotent - safe to call
 *  on every submission (new, re-submitted/merged, or already-resolved) and from the startup
 *  backfill. `read` defaults to false (a genuinely new proposal is unread) unless opts.read is set
 *  (used by the startup backfill, which marks pre-existing proposals as already seen - see below). */
function ensureForProposalIn(s, proposalId, opts = {}) {
  const existing = s.ko_notifications.find((n) => n.proposal_id === proposalId);
  if (existing) return { id: existing.id, created: false };
  const ts = nowIso();
  const read = !!opts.read;
  const row = { id: uuidv4(), proposal_id: proposalId, created_at: ts, read, read_at: read ? ts : null };
  s.ko_notifications.push(row);
  return { id: row.id, created: true };
}

function ensureForProposal(proposalId, opts) {
  return store.tx((s) => ensureForProposalIn(s, proposalId, opts));
}

/** One-time startup migration (critical requirement: "не потерять ожидающие решения предложения и
 *  не создать дубли" when this feature is deployed to an existing database). Every pre-existing
 *  ko_proposal review_queue item - open or already resolved - gets a notification row so the
 *  "ожидающих решения" count is correct from the first request; it is backfilled as *read* (not a
 *  new event to alert on - the administrator already had the "Предложения агента КО" tab for
 *  these) rather than unread. Idempotent via ensureForProposalIn, so re-running it on every boot
 *  (same call site as workflowModel.resolveStageItemsOfNonMain in server/index.js) is harmless. */
function backfillFromExistingProposals() {
  return store.tx((s) => {
    let created = 0;
    s.review_queue.filter((r) => r.kind === KIND).forEach((r) => {
      if (ensureForProposalIn(s, r.id, { read: true }).created) created += 1;
    });
    return created;
  });
}

function toApi(n, proposal) {
  return Object.assign({ notification_id: n.id, read: !!n.read, read_at: n.read_at || null, created_at: n.created_at },
    proposal || { id: n.proposal_id, kind: null, payload: {}, resolved: false, resolution: null });
}

/** status: "unread" (only unread) | "all" (default - the full history, newest first). limit caps
 *  how many rows come back (the panel's "последние" view vs. its "история" view use the same
 *  endpoint with a different limit). */
function list({ status = "all", limit = 200 } = {}) {
  return store.read((s) => {
    const rows = s.ko_notifications
      .filter((n) => status !== "unread" || !n.read)
      .map((n) => {
        const r = s.review_queue.find((x) => x.id === n.proposal_id && x.kind === KIND);
        return toApi(n, r ? JSON.parse(JSON.stringify(r)) : null);
      })
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
      .slice(0, Math.max(1, Math.min(500, Number(limit) || 200)));
    const unread_count = s.ko_notifications.filter((n) => !n.read).length;
    const pending_count = s.review_queue.filter((r) => r.kind === KIND && !r.resolved).length;
    return { notifications: rows, unread_count, pending_count };
  });
}

function markRead(id) {
  return store.tx((s) => {
    const n = s.ko_notifications.find((x) => x.id === id);
    if (!n) return false;
    if (!n.read) { n.read = true; n.read_at = nowIso(); }
    return true;
  });
}

function markAllRead() {
  return store.tx((s) => {
    const ts = nowIso();
    let n = 0;
    s.ko_notifications.forEach((row) => { if (!row.read) { row.read = true; row.read_at = ts; n += 1; } });
    return n;
  });
}

module.exports = { ensureForProposalIn, ensureForProposal, backfillFromExistingProposals, list, markRead, markAllRead, KIND };
