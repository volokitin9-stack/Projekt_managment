"use strict";

// Review queue: items that must be confirmed by a person before they become
// (or attach to) a project - unmatched photos, ambiguous planning records,
// projects created without a stable identifier. Storage: server/store.js.

const { v4: uuidv4 } = require("uuid");
const store = require("./store");

function nowIso() {
  return new Date().toISOString();
}

function toApi(r) {
  return {
    id: r.id,
    kind: r.kind,
    payload: JSON.parse(JSON.stringify(r.payload)),
    reason: r.reason || null,
    source: r.source || null,
    dedupe_key: r.dedupe_key || null,
    severity: r.severity || "review",
    created_at: r.created_at,
    updated_at: r.updated_at || r.created_at,
    resolved: !!r.resolved,
    resolved_at: r.resolved_at || null,
    resolution: r.resolution || null
  };
}

const SEVERITIES = ["blocking", "review", "warning", "info"];

/**
 * Insert, or update the item with the same dedupe key (idempotent).
 * severity: blocking | review | warning | info (default review).
 * reopen: an automatically resolved item whose problem is back is opened again
 * (items resolved by a person stay resolved).
 */
function upsertIn(s, { kind, payload, reason = null, source = null, dedupeKey = null, severity = null, reopen = false }) {
  const sev = SEVERITIES.includes(severity) ? severity : null;
  if (dedupeKey) {
    const existing = s.review_queue.find((r) => r.dedupe_key === dedupeKey);
    if (existing) {
      if (existing.resolved && reopen && existing.resolution === "auto_resolved") {
        Object.assign(existing, { resolved: false, resolved_at: null, resolution: null });
      }
      if (!existing.resolved) {
        existing.payload = JSON.parse(JSON.stringify(payload));
        existing.reason = reason;
        if (sev) existing.severity = sev;
        existing.updated_at = nowIso();
      }
      return { id: existing.id, duplicate: true, resolved: !!existing.resolved };
    }
  }
  const ts = nowIso();
  const item = { id: uuidv4(), kind, payload: JSON.parse(JSON.stringify(payload)), reason, created_at: ts,
    updated_at: ts, resolved: false, dedupe_key: dedupeKey, source, resolved_at: null, resolution: null };
  if (sev) item.severity = sev;
  s.review_queue.push(item);
  return { id: item.id, duplicate: false };
}

function upsert(opts) {
  return store.tx((s) => upsertIn(s, opts));
}

/** Resolve open items by dedupe key (the problem no longer exists). Returns how many were resolved. */
function resolveKeysIn(s, keys, resolution = "auto_resolved") {
  let n = 0;
  s.review_queue.forEach((r) => {
    if (!r.resolved && r.dedupe_key && keys.includes(r.dedupe_key)) {
      Object.assign(r, { resolved: true, resolved_at: nowIso(), resolution });
      n += 1;
    }
  });
  return n;
}

/** severity: null = actionable items (blocking + review, incl. items without a severity);
 *  "all" = everything; or one level. A kind filter returns every severity of that kind. */
function list({ includeResolved = false, kind = null, severity = null } = {}) {
  const sevOk = (r) => {
    const v = r.severity || "review";
    if (severity === "all" || (kind && !severity)) return true;
    if (severity) return v === severity;
    return v === "blocking" || v === "review";
  };
  return store.read((s) => s.review_queue
    .filter((r) => (includeResolved || !r.resolved) && (!kind || r.kind === kind) && sevOk(r))
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
    .map(toApi));
}

function resolve(id, resolution) {
  return store.tx((s) => {
    const r = s.review_queue.find((x) => x.id === id && !x.resolved);
    if (!r) return false;
    r.resolved = true;
    r.resolved_at = nowIso();
    r.resolution = resolution || "resolved";
    return true;
  });
}

module.exports = { upsert, upsertIn, resolveKeysIn, list, resolve, SEVERITIES };
