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
    created_at: r.created_at,
    updated_at: r.updated_at || r.created_at,
    resolved: !!r.resolved,
    resolved_at: r.resolved_at || null,
    resolution: r.resolution || null
  };
}

/** Insert, or update the open item with the same dedupe key (idempotent). */
function upsert({ kind, payload, reason = null, source = null, dedupeKey = null }) {
  return store.tx((s) => {
    if (dedupeKey) {
      const existing = s.review_queue.find((r) => r.dedupe_key === dedupeKey);
      if (existing) {
        if (!existing.resolved) {
          existing.payload = JSON.parse(JSON.stringify(payload));
          existing.reason = reason;
          existing.updated_at = nowIso();
        }
        return { id: existing.id, duplicate: true, resolved: !!existing.resolved };
      }
    }
    const ts = nowIso();
    const item = { id: uuidv4(), kind, payload: JSON.parse(JSON.stringify(payload)), reason, created_at: ts,
      updated_at: ts, resolved: false, dedupe_key: dedupeKey, source, resolved_at: null, resolution: null };
    s.review_queue.push(item);
    return { id: item.id, duplicate: false };
  });
}

function list({ includeResolved = false, kind = null } = {}) {
  return store.read((s) => s.review_queue
    .filter((r) => (includeResolved || !r.resolved) && (!kind || r.kind === kind))
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

module.exports = { upsert, list, resolve };
